import torch
from torch.utils.data.dataloader import DataLoader, _utils, Dataset

from typing import Any, Callable, TypeVar, Generic, Sequence, List, Optional, Iterator, Sized
from torch._six import int_classes as _int_classes
from PIL import Image
import numpy as np
T_co = TypeVar('T_co', covariant=True)
from torchvision.transforms import ToPILImage
import random
import imageio
import math

# 读取图像并转换大小
def image_transform(filename): #对OCT sacan 压缩纵轴
    image = imageio.imread(filename)
    resize_image = np.array(image)
    return resize_image

def read_imageme(scanlist):  #读取单组图像
    data3d = np.zeros((640, 304, 304), dtype=np.int)
    #print("picking ...It will take some minutes")

    #读取三维数据
    scan_num=-1
    for scan in scanlist:
        scan_num+=1
        data3d[:,:,scan_num]=image_transform(scan)  #[1,640,304,304,180]

    return data3d

class Sampler(Generic[T_co]):
    r"""Base class for all Samplers.
    """
    def __init__(self, data_source: Optional[Sized]) -> None:
        pass

    def __iter__(self) -> Iterator[T_co]:
        raise NotImplementedError


class _DatasetKind(object):
    Map = 0
    Iterable = 1

    @staticmethod
    def create_fetcher(kind, dataset, auto_collation, collate_fn, drop_last):
        if kind == _DatasetKind.Map:
            return _utils.fetch._MapDatasetFetcher(dataset, auto_collation, collate_fn, drop_last)
        else:
            return _utils.fetch._IterableDatasetFetcher(dataset, auto_collation, collate_fn, drop_last)

class _BaseDataLoaderIter(object):
    #调用第四步 dataloader.py 400
    def __init__(self, loader: DataLoader) -> None:
        self._dataset = loader.dataset
        self._drop_last = loader.drop_last
        self._index_sampler = loader._index_sampler
        self._num_workers = loader.num_workers
        self._prefetch_factor = loader.prefetch_factor
        self._pin_memory = loader.pin_memory and torch.cuda.is_available()
        self._timeout = loader.timeout
        self._collate_fn = loader.collate_fn
        self._sampler_iter = iter(self._index_sampler)
        self._base_seed = torch.empty((), dtype=torch.int64).random_(generator=loader.generator).item()
        self._persistent_workers = loader.persistent_workers
        self._num_yielded = 0   #返回调用第三步

    def __iter__(self) -> '_BaseDataLoaderIter':
        return self

    def _reset(self, loader, first_iter=False):
        self._sampler_iter = iter(self._index_sampler)
        self._num_yielded = 0
        self._IterableDataset_len_called = loader._IterableDataset_len_called

    #迭代第三步 dataloader.py 426
    def _next_index(self):
        return next(self._sampler_iter)  # may raise StopIteration 转迭代第四步

    def _next_data(self):
        raise NotImplementedError

    #迭代第一步 dataloader.py 432
    def __next__(self) -> Any:
        if self._sampler_iter is None:
            self._reset()
        data = self._next_data()    #迭代第二步
        return data

    next = __next__  # Python 2 compatibility

    def __len__(self) -> int:
        return len(self._index_sampler)

    def __getstate__(self):
        # TODO: add limited pickling support for sharing an iterator
        # across multiple threads for HOGWILD.
        # Probably the best way to do this is by moving the sample pushing
        # to a separate thread and then just sharing the data queue
        # but signalling the end is tricky without a non-blocking API
        raise NotImplementedError("{} cannot be pickled", self.__class__.__name__)

class _SingleProcessDataLoaderIter(_BaseDataLoaderIter):
    # 调用第三步 dataloader.py 464
    def __init__(self, loader,img1_trans,img2_trans):
        super(_SingleProcessDataLoaderIter, self).__init__(loader)  #转调用第四步
        assert self._timeout == 0
        assert self._num_workers == 0
        self.img1_trans = img1_trans
        self.img2_trans = img2_trans

    # 第六步
    def fetch(self, possibly_batched_index):
        # data = [self._dataset[idx] for idx in possibly_batched_index]    #转第7步
        # 第七步放在这里
        data = [[], [], []]
        # alllist = self._dataset[0][()] # ndarray转化为内置字典类型dict
        # 3D图像加载和处理
        for idx in possibly_batched_index:
            thedata = image_transform(idx)  # [640,400]
            thedata = thedata.copy()
            thedata = thedata[168:472, :] / 255.  # float64 [400,400]
            data[1].append(self.img2_trans(thedata))    #为动量编码添加原数据输入
            thedata = self.img1_trans(thedata)  #[304,304] 随机裁剪
            if torch.rand(1) < 0.5: #随机横向翻转
                thedata = torch.flip(thedata, [2])
            data[0].append(thedata)

        data[0] = torch.stack(data[0], 0)
        data[0] = data[0].type(torch.FloatTensor)

        data[1] = torch.stack(data[1], 0)
        data[1] = data[1].type(torch.FloatTensor)

        for pi in possibly_batched_index:
            for i in range(len(self._dataset[3])):  #自动根据类别数调整迭代次数
                if pi in self._dataset[3][i]:
                    data[2].append(i)

        # 对标签处理
        #data[2] = [self._dataset[1][idx] for idx in possibly_batched_index]
        data[2] = torch.tensor(np.array(data[2]))
        data[2] = data[2].type(torch.LongTensor)
        return data  # 不转第8步  转回第6步再转回第2步再转回第一步，返回主程序

    #迭代第二步 dataloader.py 473
    def _next_data(self):
        index = self._next_index()  # may raise StopIteration   转迭代第三步
        data = self.fetch(index)  # may raise StopIteration
        if self._pin_memory:
            data = _utils.pin_memory.pin_memory(data)
        return data

#初始化第1.2次 sampler.py 194
class BatchSampler(Sampler[List[int]]):
    r"""Wraps another sampler to yield a mini-batch of indices.

    Args:
        sampler (Sampler or Iterable): Base sampler. Can be any iterable object
        batch_size (int): Size of mini-batch.
        drop_last (bool): If ``True``, the sampler will drop the last batch if
            its size would be less than ``batch_size``
    """

    def __init__(self, sampler: Sampler[int], batch_size: int, drop_last: bool, dataset) -> None:
        # Since collections.abc.Iterable does not check for `__getitem__`, which
        # is one way for an object to be an iterable, we don't do an `isinstance`
        self.sampler = sampler
        self.batch_size = batch_size
        self.drop_last = drop_last
        self.clist = dataset


    #迭代第四步 sampler.py 225
    def __iter__(self):
        batch = []
        for idx in self.sampler:
            batch.append(self.clist[idx])
            if len(batch) == self.batch_size:
                yield batch
                batch = []
        if len(batch) > 0 and not self.drop_last:
            yield batch

    def __len__(self):
        # Can only be called if self.sampler has __len__ implemented
        # We cannot enforce this condition, so we turn off typechecking for the
        # implementation below.
        # Somewhat related: see NOTE [ Lack of Default `__len__` in Python Abstract Base Classes ]
        if self.drop_last:
            return len(self.sampler) // self.batch_size  # type: ignore
        else:
            return (len(self.sampler) + self.batch_size - 1) // self.batch_size  # type: ignore

class SequentialSampler(Sampler[int]):
    r"""Samples elements sequentially, always in the same order.

    Arguments:
        data_source (Dataset): dataset to sample from
    """
    data_source: Sized

    def __init__(self, data_source):
        self.data_source = data_source

    def __iter__(self):
        return iter(range(len(self.data_source)))

    def __len__(self) -> int:
        return len(self.data_source)

#初始化第1.1次 sampler.py 73
class RandomSampler(Sampler[int]):
    r"""Samples elements randomly. If without replacement, then sample from a shuffled dataset.
    If with replacement, then user can specify :attr:`num_samples` to draw.

    Arguments:
        data_source (Dataset): dataset to sample from
        replacement (bool): samples are drawn on-demand with replacement if ``True``, default=``False``
        num_samples (int): number of samples to draw, default=`len(dataset)`. This argument
            is supposed to be specified only when `replacement` is ``True``.
        generator (Generator): Generator used in sampling.
    """
    data_source: Sized
    replacement: bool

    def __init__(self, data_source: Sized, replacement: bool = False,
                 num_samples: Optional[int] = None, generator=None) -> None:
        self.data_source = data_source
        self.replacement = replacement
        self._num_samples = num_samples
        self.generator = generator

    @property
    def num_samples(self) -> int:
        # dataset size might change at runtime
        if self._num_samples is None:
            return len(self.data_source)
        return self._num_samples

    #迭代第五步 sampler.py 113
    def __iter__(self):
        n = len(self.data_source)
        if self.generator is None:
            generator = torch.Generator()
            generator.manual_seed(int(torch.empty((), dtype=torch.int64).random_().item()))
        else:
            generator = self.generator
        if self.replacement:
            for _ in range(self.num_samples // 32):
                yield from torch.randint(high=n, size=(32,), dtype=torch.int64, generator=generator).tolist()
            yield from torch.randint(high=n, size=(self.num_samples % 32,), dtype=torch.int64, generator=generator).tolist()
        else:
            yield from torch.randperm(n, generator=self.generator).tolist() #返回迭代第二步

    def __len__(self):
        return self.num_samples

#初始化第一次 dataloader.py 69
class DataLoader(Generic[T_co]):
    dataset: Dataset[T_co]
    batch_size: Optional[int]
    num_workers: int
    pin_memory: bool
    drop_last: bool
    timeout: float
    sampler: Sampler
    prefetch_factor: int
    _iterator : Optional['_BaseDataLoaderIter']
    __initialized = False

    def __init__(self, dataset, batch_size: Optional[int] = 1,
                 img1_trans = None, img2_trans = None,
                 num_workers: int = 0, collate_fn = None,
                 pin_memory: bool = False, drop_last: bool = False,
                 timeout: float = 0, worker_init_fn = None,
                 multiprocessing_context=None, generator=None,
                 *, prefetch_factor: int = 2,
                 persistent_workers: bool = False,
                 splfun = 'rand'):
        torch._C._log_api_usage_once("python.data_loader")  # type: ignore

        self.dataset = dataset
        self.num_workers = num_workers
        self.prefetch_factor = prefetch_factor
        self.pin_memory = pin_memory
        self.timeout = timeout
        self.worker_init_fn = worker_init_fn
        self.multiprocessing_context = multiprocessing_context

        allBs = []
        for x in dataset[3]:
            for xi in x:
                allBs.append(xi)

        if splfun == 'rand':
            sampler = RandomSampler(allBs, generator=generator)  # 初始化RandomSampler
        else:
            sampler = SequentialSampler(allBs)

        batch_sampler = BatchSampler(sampler, batch_size, drop_last, allBs)    #初始化BatchSampler

        self.batch_size = batch_size
        self.len = math.ceil(len(allBs) / self.batch_size)
        self.drop_last = drop_last
        self.sampler = sampler
        self.batch_sampler = batch_sampler
        self.generator = generator
        collate_fn = _utils.collate.default_collate
        self.collate_fn = collate_fn
        self.persistent_workers = persistent_workers

        self.__initialized = True
        self._IterableDataset_len_called = None  # See NOTE [ IterableDataset and __len__ ]

        self._iterator = None
        self.img1_trans = img1_trans
        self.img2_trans = img2_trans

    # 调用第二步 dataloader.py 290
    def _get_iterator(self) -> '_BaseDataLoaderIter':
        return _SingleProcessDataLoaderIter(self,self.img1_trans,self.img2_trans)   #转调用第三步

    #调用第一步 dataloader.py 339
    def __iter__(self) -> '_BaseDataLoaderIter':
        # When using a single worker the returned iterator should be
        # created everytime to avoid reseting its state
        # However, in the case of a multiple workers iterator
        # the iterator is only created once in the lifetime of the
        # DataLoader object so that workers can be reused
        return self._get_iterator() #转调用第二步

    @property
    def _auto_collation(self):
        return self.batch_sampler is not None

    @property
    def _index_sampler(self):
        # The actual sampler used for generating indices for `_DatasetFetcher`
        # (see _utils/fetch.py) to read data at each time. This would be
        # `.batch_sampler` if in auto-collation mode, and `.sampler` otherwise.
        # We can't change `.sampler` and `.batch_sampler` attributes for BC
        # reasons.
        if self._auto_collation:
            return self.batch_sampler
        else:
            return self.sampler
