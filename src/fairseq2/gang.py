# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from datetime import timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, final

import torch
import torch.distributed as dist
from torch import Tensor
from torch.distributed import Backend, ProcessGroup, ReduceOp

from fairseq2.typing import CPU, Device, override
from fairseq2.utils.logging import get_log_writer
from fairseq2.utils.version import torch_greater_or_equal

log = get_log_writer(__name__)


class ReduceOperation(Enum):
    """Specifies a reduce operation."""

    SUM = 1
    MEAN = 2
    PRODUCT = 3
    MIN = 4
    MAX = 5


class Gang(ABC):
    """Represents a set of processes that work collectively."""

    @abstractmethod
    def close(self) -> None:
        """Close and destroy the gang."""

    @abstractmethod
    def create_gang(self, ranks: Sequence[int]) -> Gang:
        """Create a new gang.

        :param ranks:
            The ranks of processes that will be part of the new gang.
        """

    @abstractmethod
    def as_process_group(self) -> ProcessGroup:
        """Return this gang as a process group."""

    @abstractmethod
    def barrier(self) -> None:
        """Synchronize all processes."""

    @abstractmethod
    def all_reduce(self, tensor: Tensor, op: ReduceOperation) -> None:
        """Reduce ``tensor`` across all processes.

        :param tensor:
            The input and output tensor of the operation.
        :param op:
            The element-wise reduce operation.
        """

    @abstractmethod
    def all_gather(self, output_tensor: Tensor, input_tensor: Tensor) -> None:
        """Gather tensors from all processes and put them in ``output_tensor``.

        :param output_tensor:
            The output tensor to accomodate tensors from all processes.
        :param input_tensor:
            The tensor to be gathered from this process.
        """

    @abstractmethod
    def all_gather_to_list(
        self, output_tensors: List[Tensor], input_tensor: Tensor
    ) -> None:
        """Gather tensors from all processes and put them in ``output_tensors``.

        :param output_tensors:
            The tensor list to accomodate tensors from all processes.
        :param input_tensor:
            The tensor to be gathered from this process.
        """

    @abstractmethod
    def broadcast_objects(self, objects: List[Any], source_rank: int = 0) -> None:
        """Broadcast picklable ``objects`` from ``source_rank``.

        :param objects:
            The list of picklable objects to broadcast. Each process must
            provide lists of equal sizes.
        :param source_rank:
            The rank of the process from which to broadcast ``objects``.
        """

    @property
    @abstractmethod
    def rank(self) -> int:
        """The rank of this process in the gang."""

    @property
    @abstractmethod
    def size(self) -> int:
        """The number of processes that are part of the gang."""

    @property
    @abstractmethod
    def device(self) -> Device:
        """The associated device."""


class AbstractGang(Gang):
    """Provides a skeletal implementation of :class:`Gang`."""

    _rank: int
    _size: int
    _device: Device

    def __init__(self, rank: int, size: int, device: Device) -> None:
        """
        :param rank:
            The rank of this process in the gang.
        :param size:
            The number of processes that are part of the gang.
        :param device:
            The associated device.
        """
        self._rank = rank
        self._size = size

        self._device = device

    @final
    @override
    def create_gang(self, ranks: Sequence[int]) -> Gang:
        if len(set(ranks)) != len(ranks):
            raise ValueError("The ranks in ``ranks`` must be all unique.")

        for idx, rank in enumerate(ranks):
            if rank < 0 or rank > self._size:
                raise ValueError(
                    f"The rank at index {idx} in ``ranks`` must be greater than or equal to 0 and less than the size of the gang ({self._size}), but is {rank} instead."
                )

        return self._do_create_gang(ranks)

    @abstractmethod
    def _do_create_gang(self, ranks: Sequence[int]) -> Gang:
        """Create a new gang.

        :param ranks:
            The ranks of processes that will be part of the new gang.
        """

    @final
    @property
    @override
    def rank(self) -> int:
        return self._rank

    @final
    @property
    @override
    def size(self) -> int:
        return self._size

    @final
    @property
    @override
    def device(self) -> Device:
        return self._device


@final
class FakeGang(AbstractGang):
    """Represents a non-distributed gang for local use."""

    def __init__(self, device: Optional[Device] = None) -> None:
        """
        :param device:
            If ``None``; if CUDA is available, the gang will use the default
            CUDA device of the process; otherwise, it will use the CPU.
        """
        if device is None:
            device = _determine_default_device()

        super().__init__(rank=0, size=1, device=device)

    @override
    def close(self) -> None:
        pass

    @override
    def _do_create_gang(self, ranks: Sequence[int]) -> FakeGang:
        return self

    @override
    def as_process_group(self) -> ProcessGroup:
        raise RuntimeError("`FakeGang` does not support conversion to a process group.")

    @override
    def barrier(self) -> None:
        pass

    @override
    def all_reduce(self, tensor: Tensor, op: ReduceOperation) -> None:
        pass

    @override
    def all_gather(self, output_tensor: Tensor, input_tensor: Tensor) -> None:
        output_tensor.copy_(input_tensor)

    @override
    def broadcast_objects(self, objects: List[Any], source_rank: int = 0) -> None:
        if source_rank != 0:
            raise ValueError(f"`source_rank` must be 0, but is {source_rank} instead.")

    @override
    def all_gather_to_list(
        self, output_tensors: List[Tensor], input_tensor: Tensor
    ) -> None:
        output_tensors[0] = input_tensor.clone().detach()


@final
class ProcessGroupGang(AbstractGang):
    """Represents a gang that wraps a process group."""

    _pg: ProcessGroup
    _debug_pg: Optional[ProcessGroup]

    def __init__(
        self, pg: ProcessGroup, device: Device, debug_pg: Optional[ProcessGroup] = None
    ) -> None:
        super().__init__(dist.get_rank(pg), dist.get_world_size(pg), device)

        self._pg = pg
        self._debug_pg = debug_pg

    @staticmethod
    def init_default_process_group(
        *,
        device: Optional[Device] = None,
        timeout: Optional[timedelta] = None,
        num_threads: Optional[int] = None,
        debug: bool = False,
        ok_initialized: bool = False,
    ) -> ProcessGroupGang:
        """Initialize the default process group and wrap it as a gang.

        :param device:
            If ``None``; if CUDA is available, the gang will use the default
            CUDA device of the process; otherwise, it will use the CPU.
        :param timeout:
            The timeout for collective operations.
        :param num_threads:
            The number of threads to use for interaop parallelism.
        :param debug:
            If ``True``, turns on additional logging and synchronization checks
            to help diagnose distributed training related issues.
        :param ok_initialized:
            If ``True``, does not raise an error if the default process group is
            already initialized.
        """
        if debug:
            os.environ["TORCH_DISTRIBUTED_DEBUG"] = "INFO"

            dist.set_debug_level_from_env()

        if not dist.is_available():
            raise RuntimeError("`torch.distributed` is not available.")

        if dist.is_initialized():
            if ok_initialized:
                return ProcessGroupGang.from_default_process_group()

            raise RuntimeError("The default process group is already initialized.")

        num_procs = get_local_world_size()

        if num_threads is None:
            if num_procs > 1 and "OMP_NUM_THREADS" not in os.environ:
                # To prevent thread oversubscription, we distribute cores evenly
                # across the workers.
                num_threads = _get_num_cpus(num_procs)

        if num_threads is not None:
            torch.set_num_threads(num_threads)

            log.info("Setting the number of threads used for intraop parallelism to {}.", num_threads)  # fmt: skip

        if device is None:
            device = _determine_default_device()

            assert device.type == "cpu" or device.type == "cuda"

        backend: Optional[str]

        if device.type == "cpu":
            backend = Backend.GLOO
        elif device.type == "cuda":
            backend = Backend.NCCL
        else:
            raise ValueError(
                f"`device` must be of type `cpu` and `cuda`, but is of type `{device.type}` instead."
            )

        if device.type == "cuda":
            nccl_env_name = "NCCL_ASYNC_ERROR_HANDLING"

            if torch_greater_or_equal(2, 2):
                try:
                    del os.environ[nccl_env_name]  # Suppress the deprecation warning.
                except KeyError:
                    pass

                nccl_env_name = "TORCH_NCCL_ASYNC_ERROR_HANDLING"

            # See https://github.com/pytorch/pytorch/issues/46874.
            os.environ[nccl_env_name] = "1"

        if timeout is None:
            timeout = timedelta(minutes=15)

        dist.init_process_group(backend, timeout=timeout)

        pg = dist.group.WORLD
        if pg is None:
            raise RuntimeError(
                "The default process group is not available. Please file a bug report."
            )

        if debug:
            if backend == Backend.GLOO:
                debug_pg = pg
            else:
                # Gloo is needed for monitored barrier support.
                debug_pg = dist.new_group(backend=Backend.GLOO, timeout=timeout)
        else:
            debug_pg = None

        return ProcessGroupGang(pg, device, debug_pg)

    @staticmethod
    def from_process_group(pg: ProcessGroup, device: Device) -> ProcessGroupGang:
        """Wrap ``pg`` as a gang.

        :param pg:
            The process group to wrap.
        :param device:
            The associated device.
        """
        return ProcessGroupGang(pg, device)

    @staticmethod
    def from_default_process_group() -> ProcessGroupGang:
        """Wrap the default process group as a gang."""
        if not dist.is_available():
            raise RuntimeError("`torch.distributed` is not available.")

        if not dist.is_initialized():
            raise RuntimeError("The default process group is not initialized.")

        backend = dist.get_backend()

        if backend == Backend.GLOO:
            device = CPU
        elif backend == Backend.NCCL:
            device = _determine_default_cuda_device()
        else:
            raise RuntimeError(
                f"Only `nccl` and `gloo` backends are supported, but the process group uses the `{backend}` backend."
            )

        if dist.group.WORLD is None:
            raise RuntimeError(
                "The default process group is not available. Please file a bug report."
            )

        return ProcessGroupGang(dist.group.WORLD, device)

    @override
    def close(self) -> None:
        dist.destroy_process_group(self._pg)

    @override
    def _do_create_gang(self, ranks: Sequence[int]) -> ProcessGroupGang:
        if self._pg is not dist.group.WORLD:
            raise RuntimeError(
                "`create_gang()` can only be called on the gang associated with the default (i.e. main) process group."
            )

        backend = dist.get_backend()

        pg = dist.new_group(ranks, backend=backend)

        if self._debug_pg is not None:
            if backend == Backend.GLOO:
                debug_pg = pg
            else:
                debug_pg = dist.new_group(ranks, backend=Backend.GLOO)
        else:
            debug_pg = None

        return ProcessGroupGang(pg, self._device, debug_pg)

    @override
    def as_process_group(self) -> ProcessGroup:
        return self._pg

    @override
    def barrier(self) -> None:
        if self._debug_pg is None:
            dist.barrier(group=self._pg, device_ids=[self._device.index])
        else:
            torch.cuda.synchronize()

            dist.monitored_barrier(group=self._debug_pg, wait_all_ranks=True)

    @override
    def all_reduce(self, tensor: Tensor, op: ReduceOperation) -> None:
        self._maybe_monitored_barrier()

        dist.all_reduce(tensor, self._get_reduce_op(op), group=self._pg)

    @override
    def all_gather(self, output_tensor: Tensor, input_tensor: Tensor) -> None:
        self._maybe_monitored_barrier()

        dist.all_gather_into_tensor(output_tensor, input_tensor, group=self._pg)

    @override
    def all_gather_to_list(
        self, output_tensors: List[Tensor], input_tensor: Tensor
    ) -> None:
        self._maybe_monitored_barrier()

        dist.all_gather(output_tensors, input_tensor, group=self._pg)

    @override
    def broadcast_objects(self, objects: List[Any], source_rank: int = 0) -> None:
        self._maybe_monitored_barrier()

        dist.broadcast_object_list(objects, source_rank)

    def _maybe_monitored_barrier(self) -> None:
        if self._debug_pg is None:
            return

        torch.cuda.synchronize()

        dist.monitored_barrier(group=self._debug_pg, wait_all_ranks=True)

    @staticmethod
    def _get_reduce_op(op: ReduceOperation):  # type: ignore[no-untyped-def]
        if op == ReduceOperation.SUM:
            return ReduceOp.SUM
        if op == ReduceOperation.MEAN:
            return ReduceOp.AVG  # type: ignore[attr-defined]
        if op == ReduceOperation.PRODUCT:
            return ReduceOp.PRODUCT
        if op == ReduceOperation.MIN:
            return ReduceOp.MIN
        if op == ReduceOperation.MAX:
            return ReduceOp.MAX

        raise ValueError(
            f"`op` must be an operation supported by the underlying process group, but is `{op}` instead."
        )


def _get_num_cpus(num_procs: int) -> int:
    num_cpus = os.cpu_count()

    affinity_mask = os.sched_getaffinity(0)

    if num_cpus is None or affinity_mask is None:
        log.warning("The number of CPUs cannot be determined.")

        return 1

    # We should not exceed the number of cores available in the affinity mask.
    return min(max(num_cpus // num_procs, 1), len(affinity_mask))


_default_device: Optional[Device] = None


def _determine_default_device() -> Device:
    global _default_device

    if _default_device is not None:
        return _default_device

    device_str = os.getenv("FAIRSEQ2_DEVICE")
    if device_str is not None:
        try:
            _default_device = Device(device_str)
        except RuntimeError as ex:
            raise RuntimeError(
                f"The value of the `FAIRSEQ2_DEVICE` environment variable must specify a valid PyTorch device, but is '{device_str}' instead."
            ) from ex

    if _default_device is None:
        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            _default_device = _determine_default_cuda_device()

    if _default_device is None:
        _default_device = CPU

    if _default_device.type == "cuda":
        torch.cuda.set_device(_default_device)

    log.info("Setting '{}' as the default device of the process.", _default_device)

    return _default_device


def _determine_default_cuda_device() -> Device:
    visible_devices = os.getenv("CUDA_VISIBLE_DEVICES")
    if visible_devices is not None:
        try:
            int(visible_devices)
        except ValueError:
            # If we are here, it means CUDA_VISIBLE_DEVICES is a list instead of
            # a single device index.
            device = None
        else:
            device = Device("cuda", index=0)
    else:
        device = None

    if device is None:
        num_devices = torch.cuda.device_count()

        idx = _get_device_index(num_devices, device_type="cuda")

        device = Device("cuda", index=idx)

    return device


def _get_device_index(num_devices: int, device_type: str) -> int:
    assert num_devices > 0

    # We use the `LOCAL_RANK` environment variable to determine which device to
    # pick in case the process has more than one available.
    device_idx = _get_int_from_env("LOCAL_RANK", allow_zero=True)
    if device_idx is None:
        num_procs = get_local_world_size()
        if num_procs > 1 and num_devices > 1:
            raise RuntimeError(
                f"The default `{device_type}` device cannot be determined. There are {num_devices} devices available, but the `LOCAL_RANK` environment variable is not set."
            )

        return 0

    if device_idx < 0:
        raise RuntimeError(
            f"The value of the `LOCAL_RANK` environment variable must be greater than or equal to 0, but is {device_idx} instead."
        )

    if device_idx >= num_devices:
        raise RuntimeError(
            f"The value of the `LOCAL_RANK` environment variable must be less than the number of available `{device_type}` devices ({num_devices}), but is {device_idx} instead."
        )

    return device_idx


def get_world_size() -> int:
    """Return the world size of the running job."""
    value = _get_int_from_env("WORLD_SIZE")

    return 1 if value is None else value


def get_rank() -> int:
    """Return the rank of this process in the running job."""
    value = _get_int_from_env("RANK", allow_zero=True)

    return 0 if value is None else value


def get_local_world_size() -> int:
    """Return the local world size of the running job."""
    value = _get_int_from_env("LOCAL_WORLD_SIZE")

    return 1 if value is None else value


def get_local_rank() -> int:
    """Return the local rank of this process in the running job."""
    value = _get_int_from_env("LOCAL_RANK", allow_zero=True)

    return 0 if value is None else value


def _get_int_from_env(var_name: str, allow_zero: bool = False) -> Optional[int]:
    s = os.getenv(var_name)
    if s is None:
        return None

    try:
        value = int(s)
    except ValueError:
        raise RuntimeError(
            f"The value of the `{var_name}` environment variable must be an integer, but is '{value}' instead."
        )

    if not allow_zero:
        if not value >= 1:
            raise RuntimeError(
                f"The value of the `{var_name}` environment variable must be greater than 0, but is {value} instead."
            )
    else:
        if not value >= 0:
            raise RuntimeError(
                f"The value of the `{var_name}` environment variable must be greater than or equal to 0, but is {value} instead."
            )

    return value


def setup_default_gang(
    *,
    device: Optional[Device] = None,
    timeout: Optional[timedelta] = None,
    debug: bool = False,
) -> Gang:
    """Set up the default gang of this process.

    :param device:
        If ``None``; if CUDA is available, the gang will use the default CUDA
        device of the process; otherwise, it will use the CPU.
    :param timeout:
        The timeout for collective operations.
    :param debug:
        If ``True``, turns on additional logging and synchronization checks
        to help diagnose distributed training related issues.
    """
    if get_world_size() == 1:
        return FakeGang(device=device)

    return ProcessGroupGang.init_default_process_group(
        device=device, timeout=timeout, debug=debug
    )


def setup_parallel_gangs(root_gang: Gang, *, tp_size: int = 1) -> Dict[str, Gang]:
    """Set up gangs to be used for data and tensor parallelism.

    For instance; if we have 8 devices denoted by g0 to g7 and 2 devices are
    used for tensor parallelism, this function will create 4 tensor parallel
    gangs and 2 data parallel gangs as:

        4 tensor parallel gangs:
            [g0, g1], [g2, g3], [g4, g5], [g6, g7]
        2 data parallel gangs:
            [g0, g2, g4, g6], [g1, g3, g5, g7]

    For efficiency, the caller should make sure adjacent ranks are on the same
    host. For example, if there are two hosts with a total of 16 GPUs, ranks 0
    to 7 belong to the first host and ranks 8 to 15 belong to the second host.

    :param root_gang:
        The gang whose topology will be used to create the new gangs.
    :param tp_size:
        The size of gangs to be used for tensor parallelism.

    :returns:
        A ``dict`` of two gangs; (1) the data parallel gang that this process
        is part of denoted by the key "dp", (2) the tensor parallel gang that
        this process is part of denoted by the key "tp".
    """
    if tp_size <= 0:
        raise ValueError(f"`tp_size` must be greater than 0, but is {tp_size} instead.")

    if root_gang.size % tp_size != 0:
        raise ValueError(
            f"`tp_size` must be divisible by `root_gang.size` ({root_gang.size}), but is {tp_size} instead."
        )

    dp_size = root_gang.size // tp_size

    if log.is_enabled_for(logging.INFO):
        for name, size in [("data", dp_size), ("tensor", tp_size)]:
            if size == 1:
                continue

            log.info("Initializing {} parallelism with a gang of size {}.", name, size)

    mesh = torch.arange(root_gang.size).view(dp_size, tp_size)

    # Get the coordinate of this process in the mesh.
    rank_coords = [x.item() for x in torch.where(mesh == root_gang.rank)]

    dp_gang: Optional[Gang] = None
    tp_gang: Optional[Gang] = None

    # Build the gangs for data parallelism.
    if dp_size == 1:
        dp_gang = FakeGang(root_gang.device)
    elif dp_size == root_gang.size:
        dp_gang = root_gang
    else:
        for i in range(tp_size):
            sub_gang = root_gang.create_gang(mesh[:, i].tolist())
            if i == rank_coords[1]:
                dp_gang = sub_gang

    # Build the gangs for tensor parallelism.
    if tp_size == 1:
        tp_gang = FakeGang(root_gang.device)
    elif tp_size == root_gang.size:
        tp_gang = root_gang
    else:
        for i in range(dp_size):
            sub_gang = root_gang.create_gang(mesh[i, :].tolist())
            if i == rank_coords[0]:
                tp_gang = sub_gang

    assert dp_gang is not None
    assert tp_gang is not None

    return {"dp": dp_gang, "tp": tp_gang}
