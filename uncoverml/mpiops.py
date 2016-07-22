import logging
import numpy as np
from mpi4py import MPI
from uncoverml import stats

log = logging.getLogger(__name__)

# MPI globals
comm = MPI.COMM_WORLD
chunks = comm.Get_size()
chunk_index = comm.Get_rank()


class PredicateGroup:
    def __init__(self, flag):
        self.flag = flag
        flag_mask = comm.allgather(flag)
        flags_with_ids = list(enumerate(flag_mask))
        true_ids = [i for i, v in flags_with_ids if v]
        false_ids = [i for i, v in flags_with_ids if not v]
        if len(true_ids) == 0:
            raise RuntimeError("run_if: all nodes have false flag")
        i = chunk_index
        self.new_index = (true_ids.index(i) if flag_mask[i] else
                          (-1 * false_ids.index(i) - 1))
        self.root_chunk = true_ids[0]

    def __enter__(self):
        self.dcomm = comm.Split(self.flag, self.new_index)
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.dcomm.Free()


def run_if(f, flag, broadcast=False, *args, **kwargs):
    result = None
    with PredicateGroup(flag) as p:
        if flag:
            if kwargs:
                kwargs.update({"comm": p.dcomm})
            else:
                kwargs = {"comm": p.dcomm}
            result = f(*args, **kwargs)
        if broadcast:
            result = comm.bcast(result, root=p.root_chunk)
    return result


def _compute_unique(x, comm, max_onehot_dims):
    x_sets = None
    # check data is okay
    if x.dtype == np.dtype('float32') or x.dtype == np.dtype('float64'):
        log.warn("Cannot use one-hot for floating point data -- ignoring")
    else:
        local_sets = stats.sets(x)
        unique_op = MPI.Op.Create(stats.unique, commute=True)
        full_sets = comm.allreduce(local_sets, op=unique_op)
        total_dims = np.sum([len(k) for k in full_sets])
        log.info("Total features from one-hot encoding: {}".format(
            total_dims))
        if total_dims <= max_onehot_dims:
            x_sets = full_sets
        else:
            log.warn("Too many distinct values for one-hot encoding.")
    return x_sets


def compute_unique_values(x, max_onehot_dims):
    flag = x is not None
    x_sets = run_if(_compute_unique, flag, x=x,
                    max_onehot_dims=max_onehot_dims,
                    broadcast=True)
    return x_sets


def run_once(f, *args, **kwargs):
    if chunk_index == 0:
        f_result = f(*args, **kwargs)
    else:
        f_result = None
    result = comm.bcast(f_result, root=0)
    return result


def sum_axis_0(x, y, dtype):
    s = np.sum(np.array([x, y]), axis=0)
    return s

sum0_op = MPI.Op.Create(sum_axis_0, commute=True)


def compose_transform(x, settings):

    flag = x is not None
    result = run_if(_compose_transform, flag, x=x,
                    settings=settings)

    flag = result is not None
    x = result[0] if flag else None
    settings = run_if(lambda r, comm: r[1], flag, r=result, broadcast=True)

    return x, settings


def _count(x, comm):
    x_n_local = np.ma.count(x, axis=0)
    x_n = comm.allreduce(x_n_local, op=sum0_op)
    return x_n


def _impute(x, settings, comm):
    if settings.impute_mean is None:
        impute_mean = _mean(x, comm)
        settings.impute_mean = impute_mean
    stats.impute_with_mean(x, settings.impute_mean)
    return x


def _mean(x, comm):
    x_n = _count(x, comm)
    x_sum_local = np.ma.sum(x, axis=0)
    if np.ma.count_masked(x_sum_local) != 0:
        raise ValueError("Too many missing values to compute sum")
    x_sum_local = x_sum_local.data
    x_sum = comm.allreduce(x_sum_local, op=sum0_op)
    mean = x_sum / x_n
    return mean


def _sd(x, comm):
    x_mean = _mean(x, comm)
    delta_mean = _mean((x - x_mean)**2, comm)
    sd = np.sqrt(delta_mean)
    return sd


def _centre(x, settings, comm):
    if settings.mean is None:
        settings.mean = _mean(x, comm)
    x -= settings.mean
    return x


def _standardise(x, settings, comm):
    x = _centre(x, settings, comm)
    if settings.sd is None:
        settings.sd = _sd(x, comm)
    x /= settings.sd
    return x


def _whiten(x, settings, comm):
    x = _centre(x, settings, comm)
    if settings.eigvals is None or settings.eigvecs is None:
        x_n = _count(x, comm)
        x_outer_local = np.ma.dot(x.T, x)
        outer = comm.allreduce(x_outer_local, op=sum0_op)
        cov = outer / x_n
        eigvals, eigvecs = np.linalg.eigh(cov)
        settings.eigvals, settings.eigvecs = eigvals, eigvecs

    ndims = x.shape[1]
    # make sure 1 <= keepdims <= ndims
    keepdims = min(max(1, int(ndims * settings.featurefraction)), ndims)
    mat = settings.eigvecs[:, -keepdims:]
    vec = settings.eigvals[-keepdims:]
    x = np.ma.dot(x, mat, strict=True) / np.sqrt(vec)
    print(mat, vec)
    return x


transform_map = {'whiten': _whiten,
                 'standardise': _standardise,
                 'centre': _centre}


def _log_missing(x, comm):
    x_n = _count(x, comm)
    x_full_local = stats.full_count(x)
    x_full = comm.allreduce(x_full_local, op=MPI.SUM)

    log.info("Total input dimensionality: {}".format(x_n.shape[0]))
    fraction_missing = (1.0 - np.sum(x_n) / (x_full * x_n.shape[0])) * 100.0
    log.info("Input data is {}% missing".format(fraction_missing))


def _compose_transform(x, settings, comm):

    _log_missing(x, comm)
    if settings.impute:
        _impute(x, settings, comm)

    f = transform_map.get(settings.transform, lambda x, *_: x)
    x = f(x, settings, comm)

    return x, settings
