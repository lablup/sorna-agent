from __future__ import annotations

from abc import ABCMeta, abstractmethod
import asyncio
from decimal import Decimal
from io import BytesIO, SEEK_END
import json
import logging
from pathlib import Path
import pickle
import pkg_resources
import platform
import re
import secrets
import signal
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Dict,
    FrozenSet,
    Generic,
    Optional,
    List,
    Literal,
    Mapping,
    MutableMapping,
    MutableSequence,
    Sequence,
    Set,
    Tuple,
    TypeVar,
    Union,
    TYPE_CHECKING,
    cast,
)

import aioredis
import aiotools
from async_timeout import timeout
import attr
from cachetools import cached, LRUCache
import snappy
import zmq, zmq.asyncio

from ai.backend.common import msgpack, redis
from ai.backend.common.docker import (
    ImageRef,
    MIN_KERNELSPEC,
    MAX_KERNELSPEC,
)
from ai.backend.common.logging import BraceStyleAdapter, pretty
from ai.backend.common.types import (
    aobject,
    # TODO: eliminate use of ContainerId
    ContainerId, KernelId,
    DeviceName, SlotName,
    AutoPullBehavior, ImageRegistry,
    ClusterInfo,
    KernelCreationConfig,
    KernelCreationResult,
    MountTypes,
    MountPermission,
    MountTuple5,
    MountTuple4,
    MountTuple3,
    Sentinel,
    ServicePortProtocols,
    SessionId,
)
from ai.backend.common.utils import current_loop
from ai.backend.common.plugin.monitor import ErrorPluginContext, StatsPluginContext
from ai.backend.common.service_ports import parse_service_ports
from . import __version__ as VERSION
from .defs import ipc_base_path
from .exception import InsufficientResource
from .kernel import (
    AbstractKernel,
    KernelFeatures,
    match_krunner_volume,
)
from .resources import (
    AbstractComputeDevice,
    AbstractComputePlugin,
    AbstractAllocMap,
    KernelResourceSpec,
    Mount,
)
from .stats import (
    StatContext, StatModes,
)
from .types import (
    Container,
    ContainerStatus,
    ContainerLifecycleEvent,
    LifecycleEvent,
)
from .utils import (
    generate_agent_id,
)

if TYPE_CHECKING:
    from ai.backend.common.etcd import AsyncEtcd

log = BraceStyleAdapter(logging.getLogger('ai.backend.agent.agent'))

_sentinel = Sentinel.TOKEN

ACTIVE_STATUS_SET = frozenset([
    ContainerStatus.RUNNING,
    ContainerStatus.RESTARTING,
    ContainerStatus.PAUSED,
])

DEAD_STATUS_SET = frozenset([
    ContainerStatus.EXITED,
    ContainerStatus.DEAD,
    ContainerStatus.REMOVING,
])


@attr.s(auto_attribs=True, slots=True)
class KernelCreationContext:
    kspec_version: int
    kernel_id: KernelId
    kernel_config: KernelCreationConfig
    kernel_features: FrozenSet[str]
    image_ref: ImageRef
    internal_data: Mapping[str, Any]
    restarting: bool
    cancellation_handlers: Sequence[Callable[[], Awaitable[None]]]


KernelCreationContextType = TypeVar('KernelCreationContextType', bound=KernelCreationContext)
KernelObjectType = TypeVar('KernelObjectType', bound=AbstractKernel)


@attr.s(auto_attribs=True, slots=True)
class RestartTracker:
    request_lock: asyncio.Lock
    destroy_event: asyncio.Event
    done_event: asyncio.Event


@attr.s(auto_attribs=True, slots=True)
class ComputerContext:
    instance: AbstractComputePlugin
    devices: Collection[AbstractComputeDevice]
    alloc_map: AbstractAllocMap


class AbstractAgent(aobject, Generic[KernelObjectType, KernelCreationContextType], metaclass=ABCMeta):

    loop: asyncio.AbstractEventLoop
    config: Mapping[str, Any]
    etcd: AsyncEtcd
    agent_id: str
    kernel_registry: MutableMapping[KernelId, AbstractKernel]
    computers: MutableMapping[str, ComputerContext]
    images: Mapping[str, str]
    port_pool: Set[int]

    redis: aioredis.Redis
    zmq_ctx: zmq.asyncio.Context

    restarting_kernels: MutableMapping[KernelId, RestartTracker]
    timer_tasks: MutableSequence[asyncio.Task]
    container_lifecycle_queue: 'asyncio.Queue[Union[ContainerLifecycleEvent, Sentinel]]'

    stat_ctx: StatContext
    stat_sync_sockpath: Path
    stat_sync_task: asyncio.Task

    stats_monitor: StatsPluginContext
    error_monitor: ErrorPluginContext

    def __init__(
        self,
        etcd: AsyncEtcd,
        local_config: Mapping[str, Any],
        *,
        stats_monitor: StatsPluginContext,
        error_monitor: ErrorPluginContext,
        skip_initial_scan: bool = False,
    ) -> None:
        self._skip_initial_scan = skip_initial_scan
        self.loop = current_loop()
        self.etcd = etcd
        self.local_config = local_config
        self.agent_id = generate_agent_id(__file__)
        self.kernel_registry = {}
        self.computers = {}
        self.images = {}  # repoTag -> digest
        self.restarting_kernels = {}
        self.stat_ctx = StatContext(
            self, mode=StatModes(local_config['container']['stats-type']),
        )
        self.timer_tasks = []
        self.port_pool = set(range(
            local_config['container']['port-range'][0],
            local_config['container']['port-range'][1] + 1,
        ))
        self.stats_monitor = stats_monitor
        self.error_monitor = error_monitor

    async def __ainit__(self) -> None:
        """
        An implementation of AbstractAgent would define its own ``__ainit__()`` method.
        It must call this super method in an appropriate order, only once.
        """
        self.resource_lock = asyncio.Lock()
        self.container_lifecycle_queue = asyncio.Queue()
        self.producer_lock = asyncio.Lock()
        self.redis_producer_pool = await redis.connect_with_retries(
            self.local_config['redis']['addr'].as_sockaddr(),
            db=4,  # REDIS_STREAM_DB in gateway.defs
            password=(self.local_config['redis']['password']
                      if self.local_config['redis']['password'] else None),
            encoding=None,
        )
        self.redis_stat_pool = await redis.connect_with_retries(
            self.local_config['redis']['addr'].as_sockaddr(),
            db=0,  # REDIS_STAT_DB in backend.ai-manager
            password=(self.local_config['redis']['password']
                      if self.local_config['redis']['password'] else None),
            encoding='utf8',
        )

        ipc_base_path.mkdir(parents=True, exist_ok=True)
        self.zmq_ctx = zmq.asyncio.Context()

        computers, self.slots = await self.detect_resources()
        for name, computer in computers.items():
            devices = await computer.list_devices()
            alloc_map = await computer.create_alloc_map()
            self.computers[name] = ComputerContext(computer, devices, alloc_map)

        if not self._skip_initial_scan:
            self.images = await self.scan_images()
            self.timer_tasks.append(aiotools.create_timer(self._scan_images_wrapper, 20.0))
            await self.scan_running_kernels()

        # Prepare stat collector tasks.
        self.timer_tasks.append(aiotools.create_timer(self.collect_node_stat, 5.0))
        self.timer_tasks.append(aiotools.create_timer(self.collect_container_stat, 5.0))

        # Prepare heartbeats.
        self.timer_tasks.append(aiotools.create_timer(self.heartbeat, 3.0))

        # Prepare auto-cleaning of idle kernels.
        self.timer_tasks.append(aiotools.create_timer(self.sync_container_lifecycles, 10.0))

        loop = current_loop()
        self.container_lifecycle_handler = loop.create_task(self.process_lifecycle_events())

        # Notify the gateway.
        await self.produce_event('instance_started', 'self-started')

    async def shutdown(self, stop_signal: signal.Signals) -> None:
        """
        An implementation of AbstractAgent would define its own ``shutdown()`` method.
        It must call this super method in an appropriate order, only once.
        """
        # Close all pending kernel runners.
        for kernel_obj in self.kernel_registry.values():
            if kernel_obj.runner is not None:
                await kernel_obj.runner.close()
            await kernel_obj.close()
        if stop_signal == signal.SIGTERM:
            await self.clean_all_kernels(blocking=True)

        # Stop timers.
        for task in self.timer_tasks:
            task.cancel()
        timer_cancel_results = await asyncio.gather(*self.timer_tasks, return_exceptions=True)
        for result in timer_cancel_results:
            if isinstance(result, Exception):
                log.error('timer cancellation error: {}', result)

        # Stop lifecycle event handler.
        await self.container_lifecycle_queue.put(_sentinel)
        await self.container_lifecycle_handler

        # Notify the gateway.
        await self.produce_event('instance_terminated', 'shutdown')

        # Close Redis connection pools.
        self.redis_producer_pool.close()
        await self.redis_producer_pool.wait_closed()
        self.redis_stat_pool.close()
        await self.redis_stat_pool.wait_closed()

    async def produce_event(self, event_name: str, *args) -> None:
        """
        Send an event to the manager(s).
        """
        if self.local_config['debug']['log-heartbeats']:
            _log = log.debug if event_name == 'instance_heartbeat' else log.info
        else:
            _log = (lambda *args: None) if event_name == 'instance_heartbeat' else log.info
        if event_name.startswith('kernel_') and len(args) > 0:
            _log('produce_event({0}, k:{1})', event_name, args[0])
        else:
            _log('produce_event({0})', event_name)
        encoded_event = msgpack.packb({
            'event_name': event_name,
            'agent_id': self.local_config['agent']['id'],
            'args': args,
        })
        async with self.producer_lock:
            def _pipe_builder():
                pipe = self.redis_producer_pool.pipeline()
                pipe.rpush('events.prodcons', encoded_event)
                pipe.publish('events.pubsub', encoded_event)
                return pipe
            await redis.execute_with_retries(_pipe_builder)

    async def heartbeat(self, interval: float):
        """
        Send my status information and available kernel images to the manager(s).
        """
        res_slots = {}
        for cctx in self.computers.values():
            for slot_key, slot_type in cctx.instance.slot_types:
                res_slots[slot_key] = (
                    slot_type,
                    str(self.slots.get(slot_key, 0)),
                )
        agent_info = {
            'ip': str(self.local_config['agent']['rpc-listen-addr'].host),
            'region': self.local_config['agent']['region'],
            'scaling_group': self.local_config['agent']['scaling-group'],
            'addr': f"tcp://{self.local_config['agent']['rpc-listen-addr']}",
            'resource_slots': res_slots,
            'version': VERSION,
            'compute_plugins': {
                key: {
                    'version': computer.instance.get_version(),
                    **(await computer.instance.extra_info())
                }
                for key, computer in self.computers.items()
            },
            'images': snappy.compress(msgpack.packb([
                (repo_tag, digest) for repo_tag, digest in self.images.items()
            ])),
        }
        try:
            await self.produce_event('instance_heartbeat', agent_info)
        except asyncio.TimeoutError:
            log.warning('event dispatch timeout: instance_heartbeat')
        except Exception:
            log.exception('instance_heartbeat failure')
            await self.error_monitor.capture_exception()

    async def collect_logs(
        self,
        kernel_id: KernelId,
        container_id: str,
        async_log_iterator: AsyncIterator[bytes],
    ) -> None:
        chunk_size = self.local_config['agent']['container-logs']['chunk-size']
        log_key = f'containerlog.{container_id}'
        log_length = 0
        chunk_buffer = BytesIO()
        chunk_length = 0
        try:
            async for fragment in async_log_iterator:
                fragment_length = len(fragment)
                chunk_buffer.write(fragment)
                chunk_length += fragment_length
                log_length += fragment_length
                while chunk_length >= chunk_size:
                    cb = chunk_buffer.getbuffer()
                    stored_chunk = bytes(cb[:chunk_size])
                    await redis.execute_with_retries(
                        lambda: self.redis_producer_pool.rpush(
                            log_key, stored_chunk)
                    )
                    remaining = cb[chunk_size:]
                    chunk_length = len(remaining)
                    next_chunk_buffer = BytesIO(remaining)
                    next_chunk_buffer.seek(0, SEEK_END)
                    del remaining, cb
                    chunk_buffer.close()
                    chunk_buffer = next_chunk_buffer
            assert chunk_length < chunk_size
            if chunk_length > 0:
                await redis.execute_with_retries(
                    lambda: self.redis_producer_pool.rpush(
                        log_key, chunk_buffer.getvalue())
                )
        finally:
            chunk_buffer.close()
        # Keep the log for at most one hour in Redis.
        # This is just a safety measure to prevent memory leak in Redis
        # for cases when the event delivery has failed or processing
        # the log data has failed.
        await redis.execute_with_retries(
            lambda: self.redis_producer_pool.expire(log_key, 3600.0)
        )
        await self.produce_event(
            'kernel_log', str(kernel_id), container_id
        )

    async def collect_node_stat(self, interval: float):
        if self.local_config['debug']['log-stats']:
            log.debug('collecting node statistics')
        try:
            await self.stat_ctx.collect_node_stat()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception('unhandled exception while syncing node stats')
            await self.error_monitor.capture_exception()

    async def collect_container_stat(self, interval: float):
        if self.local_config['debug']['log-stats']:
            log.debug('collecting container statistics')
        try:
            updated_kernel_ids = []
            for kernel_id, kernel_obj in [*self.kernel_registry.items()]:
                if not kernel_obj.stats_enabled:
                    continue
                updated_kernel_ids.append(kernel_id)
                cid = kernel_obj['container_id']
                await self.stat_ctx.collect_container_stat(cid)
            # Let the manager store the statistics in the persistent database.
            if updated_kernel_ids:
                await self.produce_event('kernel_stat_sync',
                                         ','.join(map(str, updated_kernel_ids)))
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception('unhandled exception while syncing container stats')
            await self.error_monitor.capture_exception()

    async def _handle_start_event(self, ev: ContainerLifecycleEvent) -> None:
        kernel_obj = self.kernel_registry.get(ev.kernel_id)
        if kernel_obj is not None:
            kernel_obj.stats_enabled = True

    async def _handle_destroy_event(self, ev: ContainerLifecycleEvent) -> None:
        result = None
        try:
            kernel_obj = self.kernel_registry.get(ev.kernel_id)
            if kernel_obj is None:
                log.warning('destroy_kernel(k:{0}) kernel missing (already dead?)',
                            ev.kernel_id)
                if ev.container_id is None:
                    await self.rescan_resource_usage()
                    await self.produce_event(
                        'kernel_terminated', str(ev.kernel_id),
                        'already-terminated', None,
                    )
                    return
                else:
                    self.container_lifecycle_queue.put_nowait(
                        ContainerLifecycleEvent(
                            ev.kernel_id,
                            ev.container_id,
                            LifecycleEvent.CLEAN,
                            ev.reason,
                        )
                    )
            else:
                kernel_obj.stats_enabled = False
                kernel_obj.termination_reason = ev.reason
                if kernel_obj.runner is not None:
                    await kernel_obj.runner.close()
            result = await self.destroy_kernel(ev.kernel_id, ev.container_id)
        except Exception:
            log.exception('unhandled exception while processing DESTROY event')
            await self.error_monitor.capture_exception()
        finally:
            if ev.done_event is not None:
                ev.done_event.set()
                setattr(ev.done_event, '_result', result)

    async def _handle_clean_event(self, ev: ContainerLifecycleEvent) -> None:
        result = None
        try:
            kernel_obj = self.kernel_registry.get(ev.kernel_id)
            if kernel_obj is not None and kernel_obj.runner is not None:
                await kernel_obj.runner.close()
            result = await self.clean_kernel(
                ev.kernel_id,
                ev.container_id,
                ev.kernel_id in self.restarting_kernels,
            )
        except Exception:
            log.exception('unhandled exception while processing CLEAN event')
            await self.error_monitor.capture_exception()
        finally:
            try:
                kernel_obj = self.kernel_registry.get(ev.kernel_id)
                if kernel_obj is not None:
                    # Restore used ports to the port pool.
                    port_range = self.local_config['container']['port-range']
                    # Exclude out-of-range ports, because when the agent restarts
                    # with a different port range, existing containers' host ports
                    # may not belong to the new port range.
                    restored_ports = [*filter(
                        lambda p: port_range[0] <= p <= port_range[1],
                        kernel_obj['host_ports']
                    )]
                    self.port_pool.update(restored_ports)
                    await kernel_obj.close()
                    # Notify cleanup waiters.
                    if kernel_obj.clean_event is not None:
                        kernel_obj.clean_event.set()
                    # Forget.
                    self.kernel_registry.pop(ev.kernel_id, None)
            finally:
                if ev.done_event is not None:
                    ev.done_event.set()
                    setattr(ev.done_event, '_result', result)
                if restart_tracker := self.restarting_kernels.get(ev.kernel_id, None):
                    restart_tracker.destroy_event.set()
                else:
                    await self.rescan_resource_usage()
                    await self.produce_event(
                        'kernel_terminated', str(ev.kernel_id),
                        ev.reason, None,
                    )

    async def process_lifecycle_events(self) -> None:
        while True:
            ev = await self.container_lifecycle_queue.get()
            if isinstance(ev, Sentinel):
                with open(ipc_base_path / f'last_registry.{self.agent_id}.dat', 'wb') as f:
                    pickle.dump(self.kernel_registry, f)
                return
            # attr currently does not support customizing getstate/setstate dunder methods
            # until the next release.
            log.info(f'lifecycle event: {ev!r}')
            try:
                if ev.event == LifecycleEvent.START:
                    asyncio.create_task(self._handle_start_event(ev))
                elif ev.event == LifecycleEvent.DESTROY:
                    asyncio.create_task(self._handle_destroy_event(ev))
                elif ev.event == LifecycleEvent.CLEAN:
                    asyncio.create_task(self._handle_clean_event(ev))
                else:
                    log.warning('unsupported lifecycle event: {!r}', ev)
            except Exception:
                log.exception('unexpected error in process_lifecycle_events(), continuing...')
            finally:
                self.container_lifecycle_queue.task_done()

    async def inject_container_lifecycle_event(
        self,
        kernel_id: KernelId,
        event: LifecycleEvent,
        reason: str,
        *,
        container_id: ContainerId = None,
        exit_code: int = None,
        done_event: asyncio.Event = None,
        clean_event: asyncio.Event = None,
    ) -> None:
        try:
            kernel_obj = self.kernel_registry[kernel_id]
            if kernel_obj.termination_reason:
                reason = kernel_obj.termination_reason
            if kernel_obj.clean_event is not None:
                # This should not happen!
                log.warning('overwriting kernel_obj.clean_event (k:{})', kernel_id)
            kernel_obj.clean_event = clean_event
            if container_id is not None and container_id != kernel_obj['container_id']:
                # This should not happen!
                log.warning('container id mismatch for kernel_obj (k:{}, c:{}) with event (c:{})',
                            kernel_id, kernel_obj['container_id'], container_id)
            container_id = kernel_obj['container_id']
        except KeyError:
            pass
        self.container_lifecycle_queue.put_nowait(
            ContainerLifecycleEvent(
                kernel_id,
                container_id,
                event,
                reason,
                done_event,
                exit_code,
            )
        )

    @abstractmethod
    async def enumerate_containers(
        self,
        status_filter: FrozenSet[ContainerStatus] = ACTIVE_STATUS_SET,
    ) -> Sequence[Tuple[KernelId, Container]]:
        """
        Enumerate the containers with the given status filter.
        """

    async def rescan_resource_usage(self) -> None:
        async with self.resource_lock:
            for computer_set in self.computers.values():
                computer_set.alloc_map.clear()
            for kernel_id, container in (await self.enumerate_containers()):
                for computer_set in self.computers.values():
                    await computer_set.instance.restore_from_container(
                        container,
                        computer_set.alloc_map,
                    )

    async def sync_container_lifecycles(self, interval: float) -> None:
        """
        Periodically synchronize the alive/known container sets,
        for cases when we miss the container lifecycle events from the underlying implementation APIs
        due to the agent restarts or crashes.
        """
        known_kernels: Dict[KernelId, ContainerId] = {}
        alive_kernels: Dict[KernelId, ContainerId] = {}
        terminated_kernels = {}

        async with self.resource_lock:
            for kernel_id, container in (await self.enumerate_containers(ACTIVE_STATUS_SET)):
                alive_kernels[kernel_id] = container.id
            for kernel_id, kernel_obj in self.kernel_registry.items():
                known_kernels[kernel_id] = kernel_obj['container_id']
            try:
                # Check if: kernel_registry has the container but it's gone.
                for kernel_id in (known_kernels.keys() - alive_kernels.keys()):
                    if kernel_id in self.restarting_kernels:
                        continue
                    terminated_kernels[kernel_id] = ContainerLifecycleEvent(
                        kernel_id,
                        known_kernels[kernel_id],
                        LifecycleEvent.CLEAN,
                        'self-terminated',
                    )
                # Check if: there are containers not spawned by me.
                for kernel_id in (alive_kernels.keys() - known_kernels.keys()):
                    if kernel_id in self.restarting_kernels:
                        continue
                    terminated_kernels[kernel_id] = ContainerLifecycleEvent(
                        kernel_id,
                        alive_kernels[kernel_id],
                        LifecycleEvent.DESTROY,
                        'terminated-unknown-container',
                    )
            finally:
                # Enqueue the events.
                for kernel_id, ev in terminated_kernels.items():
                    self.container_lifecycle_queue.put_nowait(ev)

    async def clean_all_kernels(self, blocking: bool = False) -> None:
        kernel_ids = [*self.kernel_registry.keys()]
        clean_events = {}
        if blocking:
            for kernel_id in kernel_ids:
                clean_events[kernel_id] = asyncio.Event()
        for kernel_id in kernel_ids:
            await self.inject_container_lifecycle_event(
                kernel_id,
                LifecycleEvent.DESTROY,
                'agent-termination',
                clean_event=clean_events[kernel_id] if blocking else None,
            )
        if blocking:
            waiters = [clean_events[kernel_id].wait()
                       for kernel_id in kernel_ids]
            await asyncio.gather(*waiters)

    @abstractmethod
    async def detect_resources(self) -> Tuple[
        Mapping[DeviceName, AbstractComputePlugin],
        Mapping[SlotName, Decimal]
    ]:
        """
        Scan and define the amount of available resource slots in this node.
        """

    @abstractmethod
    async def scan_images(self) -> Mapping[str, str]:
        """
        Scan the available kernel images/templates and update ``self.images``.
        This is called periodically to keep the image list up-to-date and allow
        manual image addition and deletions by admins.
        """

    async def _scan_images_wrapper(self, interval: float) -> None:
        self.images = await self.scan_images()

    @abstractmethod
    async def pull_image(self, image_ref: ImageRef, registry_conf: ImageRegistry) -> None:
        '''
        Pull the given image from the given registry.
        '''

    @abstractmethod
    async def check_image(self, image_ref: ImageRef, image_id: str, auto_pull: AutoPullBehavior) -> bool:
        '''
        Check the availability of the image and return a boolean flag that indicates whether
        the agent should try pulling the image from a registry.
        '''
        return False

    async def scan_running_kernels(self) -> None:
        """
        Scan currently running kernels and recreate the kernel objects in
        ``self.kernel_registry`` if any missing.
        """
        try:
            with open(ipc_base_path / f'last_registry.{self.agent_id}.dat', 'rb') as f:
                self.kernel_registry = pickle.load(f)
                for kernel_obj in self.kernel_registry.values():
                    kernel_obj.agent_config = self.local_config
                    if kernel_obj.runner is not None:
                        await kernel_obj.runner.__ainit__()
        except FileNotFoundError:
            pass
        async with self.resource_lock:
            for kernel_id, container in (await self.enumerate_containers(
                ACTIVE_STATUS_SET | DEAD_STATUS_SET,
            )):
                if container.status in ACTIVE_STATUS_SET:
                    kernelspec = int(container.labels.get('ai.backend.kernelspec', '1'))
                    if not (MIN_KERNELSPEC <= kernelspec <= MAX_KERNELSPEC):
                        continue
                    # Consume the port pool.
                    for p in container.ports:
                        if p.host_port is not None:
                            self.port_pool.discard(p.host_port)
                    # Restore compute resources.
                    for computer_set in self.computers.values():
                        await computer_set.instance.restore_from_container(
                            container,
                            computer_set.alloc_map,
                        )
                    await self.inject_container_lifecycle_event(
                        kernel_id,
                        LifecycleEvent.START,
                        'resuming-agent-operation',
                        container_id=container.id,
                    )
                elif container.status in DEAD_STATUS_SET:
                    log.info('detected dead container while agent is down (k:{0}, c:{})',
                             kernel_id, container.id)
                    await self.inject_container_lifecycle_event(
                        kernel_id,
                        LifecycleEvent.CLEAN,
                        'self-terminated',
                        container_id=container.id,
                    )

        log.info('starting with resource allocations')
        for computer_name, computer_ctx in self.computers.items():
            log.info('{}: {!r}',
                     computer_name,
                     dict(computer_ctx.alloc_map.allocations))

    async def create_kernel__init_context(
        self,
        kernel_id: KernelId,
        kernel_config: KernelCreationConfig,
        *,
        restarting: bool = False,
    ) -> KernelCreationContextType:
        image_ref = ImageRef(
            kernel_config['image']['canonical'],
            [kernel_config['image']['registry']['name']],
        )
        image_labels = kernel_config['image']['labels']
        version = int(image_labels.get('ai.backend.kernelspec', '1'))
        kernel_features = frozenset(image_labels.get('ai.backend.features', '').split())
        return cast(KernelCreationContextType, KernelCreationContext(
            kspec_version=version,
            kernel_features=kernel_features,
            kernel_id=kernel_id,
            kernel_config=kernel_config,
            image_ref=image_ref,
            internal_data=kernel_config['internal_data'] or {},
            restarting=restarting,
            cancellation_handlers=[],
        ))

    @abstractmethod
    async def create_kernel__get_extra_envs(
        self,
        ctx: KernelCreationContextType,
    ) -> Mapping[str, str]:
        return {}

    @abstractmethod
    async def create_kernel__prepare_resource_spec(
        self,
        ctx: KernelCreationContextType,
    ) -> Tuple[KernelResourceSpec, Optional[Mapping[str, Any]]]:
        raise NotImplementedError

    @abstractmethod
    async def create_kernel__prepare_scratch(
        self,
        ctx: KernelCreationContextType,
    ) -> None:
        pass

    @abstractmethod
    async def create_kernel__get_intrinsic_mounts(
        self,
        ctx: KernelCreationContextType,
    ) -> Sequence[Mount]:
        return []

    @abstractmethod
    async def create_kernel__apply_network(
        self,
        ctx: KernelCreationContextType,
        cluster_info: ClusterInfo,
    ) -> None:
        """
        Apply the given cluster network information to the deployment.
        """
        raise NotImplementedError

    @abstractmethod
    async def create_kernel__install_ssh_keypair(
        self,
        ctx: KernelCreationContextType,
        cluster_info: ClusterInfo,
    ) -> None:
        """
        Install the ssh keypair inside the kernel from cluster_info.
        """
        raise NotImplementedError

    @abstractmethod
    async def create_kernel__process_mounts(
        self,
        ctx: KernelCreationContextType,
        mounts: Sequence[Mount],
    ):
        raise NotImplementedError

    @abstractmethod
    async def create_kernel__apply_accelerator_allocation(
        self,
        ctx: KernelCreationContextType,
        computer,
        device_alloc,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def create_kernel__spawn(
        self,
        ctx: KernelCreationContextType,
        resource_spec: KernelResourceSpec,
        resource_opts,
        environ: Mapping[str, str],
        service_ports,
        preopen_ports,
        cmdargs: List[str],
    ) -> KernelObjectType:
        raise NotImplementedError

    async def _create_kernel__mount_vfolders(
        self,
        ctx: KernelCreationContextType,
        vfolders,
        resource_spec: KernelResourceSpec,
    ) -> None:
        vfolder_mount_map: Mapping[str, str]
        vfolder_mount_map = ctx.kernel_config.get('mount_map', {})
        for vfolder in vfolders:
            is_unmanaged = False
            # TODO: update to use storage-proxy-provided mount path
            if len(vfolder) == 5:
                folder_name, folder_host, folder_id, folder_perm_literal, host_path_raw = \
                    cast(MountTuple5, vfolder)
                if host_path_raw:
                    is_unmanaged = True
                    host_path = Path(host_path_raw)
                else:
                    mount_path = Path(folder_id)
                    if mount_path.is_absolute():
                        # Use the storage proxy-provided path as-is.
                        host_path = mount_path
                    else:
                        host_path = (self.local_config['vfolder']['mount'] / folder_host /
                                     self.local_config['vfolder']['fsprefix'] / folder_id)
            elif len(vfolder) == 4:  # for backward compatibility
                folder_name, folder_host, folder_id, folder_perm_literal = \
                    cast(MountTuple4, vfolder)
                host_path = (self.local_config['vfolder']['mount'] / folder_host /
                             self.local_config['vfolder']['fsprefix'] / folder_id)
            elif len(vfolder) == 3:  # legacy managers
                folder_name, folder_host, folder_id = cast(MountTuple3, vfolder)
                folder_perm_literal = 'rw'
                host_path = (self.local_config['vfolder']['mount'] / folder_host /
                             self.local_config['vfolder']['fsprefix'] / folder_id)
            else:
                raise RuntimeError(
                    'Unexpected number of vfolder mount detail tuple size')
            if ctx.internal_data.get('prevent_vfolder_mounts', False):
                # Only allow mount of ".logs" directory to prevent expose
                # internal-only information, such as Docker credentials to user's ".docker" vfolder
                # in image importer kernels.
                if folder_name != '.logs':
                    continue
            if kernel_path_raw := vfolder_mount_map.get(folder_name):
                if not kernel_path_raw.startswith('/home/work/'):  # type: ignore
                    raise ValueError(
                        f'Error while mounting {folder_name} to {kernel_path_raw}: '
                        'all vfolder mounts should be under /home/work')
                kernel_path = Path(kernel_path_raw)  # type: ignore
            else:
                kernel_path = Path(f'/home/work/{folder_name}')
            folder_perm = MountPermission(folder_perm_literal)
            mount = Mount(
                MountTypes.BIND,
                host_path,
                kernel_path,
                folder_perm,
                is_unmanaged=is_unmanaged,
            )
            resource_spec.mounts.append(mount)

    @cached(
        cache=LRUCache(maxsize=32),  # type: ignore
        key=lambda self, ctx: (
            ctx.image_ref,
            ctx.kernel_config['image']['labels'].get('ai.backend.base-distro', 'ubuntu16.04'),
        ),
    )
    def _create_kernel__get_krunner_info(
        self,
        ctx: KernelCreationContextType,
    ) -> Tuple[str, str, str, str, str]:
        image_labels = ctx.kernel_config['image']['labels']
        distro = image_labels.get('ai.backend.base-distro', 'ubuntu16.04')
        matched_distro, krunner_volume = match_krunner_volume(
            self.local_config['container']['krunner-volumes'], distro)
        matched_libc_style = 'glibc'
        if matched_distro.startswith('alpine'):
            matched_libc_style = 'musl'
        krunner_pyver = '3.6'  # fallback
        if m := re.search(r'^([a-z]+)\d+(\.\d+)*$', matched_distro):
            matched_distro_type = m.group(1)
            try:
                krunner_pyver = Path(pkg_resources.resource_filename(
                    f'ai.backend.krunner.{matched_distro_type}',
                    f'krunner-python.{matched_distro}.txt',
                )).read_text().strip()
            except FileNotFoundError:
                pass
        log.debug('selected krunner: {}', matched_distro)
        log.debug('selected libc style: {}', matched_libc_style)
        log.debug('krunner volume: {}', krunner_volume)
        log.debug('krunner python: {}', krunner_pyver)
        arch = platform.machine()
        return arch, matched_distro, matched_libc_style, krunner_volume, krunner_pyver

    async def _create_kernel__mount_krunner(
        self,
        ctx: KernelCreationContextType,
        resource_spec: KernelResourceSpec,
        environ: MutableMapping[str, str],
    ) -> None:

        def _mount(
            type: MountTypes,
            src: Union[str, Path],
            target: Union[str, Path],
            perm: Literal['ro', 'rw'] = 'ro',
            is_unmanaged: bool = False,
            opts: Mapping[str, Any] = None,
        ) -> None:
            resource_spec.mounts.append(
                Mount(type, Path(src), Path(target),
                      MountPermission(perm),
                      is_unmanaged=is_unmanaged,
                      opts=opts)
            )

        # Inject Backend.AI kernel runner dependencies.
        arch, matched_distro, matched_libc_style, krunner_volume, krunner_pyver = \
            self._create_kernel__get_krunner_info(ctx)
        entrypoint_sh_path = Path(pkg_resources.resource_filename(
            'ai.backend.agent', '../runner/entrypoint.sh'))
        if matched_distro == 'centos6.10':
            # special case for image importer kernel (manylinux2010 is based on CentOS 6)
            suexec_path = Path(pkg_resources.resource_filename(
                'ai.backend.runner', f'su-exec.centos7.6.{arch}.bin'))
            hook_path = Path(pkg_resources.resource_filename(
                'ai.backend.runner', f'libbaihook.centos7.6.{arch}.so'))
            sftp_server_path = Path(pkg_resources.resource_filename(
                'ai.backend.runner',
                f'sftp-server.centos7.6.{arch}.bin'))
            scp_path = Path(pkg_resources.resource_filename(
                'ai.backend.runner',
                f'scp.centos7.6.{arch}.bin'))
        else:
            suexec_path = Path(pkg_resources.resource_filename(
                'ai.backend.runner', f'su-exec.{matched_distro}.{arch}.bin'))
            hook_path = Path(pkg_resources.resource_filename(
                'ai.backend.runner', f'libbaihook.{matched_distro}.{arch}.so'))
            sftp_server_path = Path(pkg_resources.resource_filename(
                'ai.backend.runner',
                f'sftp-server.{matched_distro}.{arch}.bin'))
            scp_path = Path(pkg_resources.resource_filename(
                'ai.backend.runner',
                f'scp.{matched_distro}.{arch}.bin'))
        if self.local_config['container']['sandbox-type'] == 'jail':
            jail_path = Path(pkg_resources.resource_filename(
                'ai.backend.runner', f'jail.{matched_distro}.bin'))
        kernel_pkg_path = Path(pkg_resources.resource_filename(
            'ai.backend.agent', '')).parent / 'kernel'
        helpers_pkg_path = Path(pkg_resources.resource_filename(
            'ai.backend.agent', '')).parent / 'helpers'
        dropbear_path = Path(pkg_resources.resource_filename(
            'ai.backend.runner',
            f'dropbear.{matched_libc_style}.{arch}.bin'))
        dropbearconv_path = Path(pkg_resources.resource_filename(
            'ai.backend.runner',
            f'dropbearconvert.{matched_libc_style}.{arch}.bin'))
        dropbearkey_path = Path(pkg_resources.resource_filename(
            'ai.backend.runner',
            f'dropbearkey.{matched_libc_style}.{arch}.bin'))
        tmux_path = Path(pkg_resources.resource_filename(
            'ai.backend.runner', f'tmux.{matched_libc_style}.{arch}.bin'))
        dotfile_extractor_path = Path(pkg_resources.resource_filename(
            'ai.backend.runner', 'extract_dotfiles.py'
        ))

        if matched_libc_style == 'musl':
            terminfo_path = Path(pkg_resources.resource_filename(
                'ai.backend.runner', 'terminfo.alpine3.8'
            ))
            _mount(MountTypes.BIND, terminfo_path.resolve(), '/home/work/.terminfo')

        _mount(MountTypes.BIND, dotfile_extractor_path.resolve(), '/opt/kernel/extract_dotfiles.py')
        _mount(MountTypes.BIND, entrypoint_sh_path.resolve(), '/opt/kernel/entrypoint.sh')
        _mount(MountTypes.BIND, suexec_path.resolve(), '/opt/kernel/su-exec')
        if self.local_config['container']['sandbox-type'] == 'jail':
            _mount(MountTypes.BIND, jail_path.resolve(), '/opt/kernel/jail')
        _mount(MountTypes.BIND, hook_path.resolve(), '/opt/kernel/libbaihook.so')
        _mount(MountTypes.BIND, dropbear_path.resolve(), '/opt/kernel/dropbear')
        _mount(MountTypes.BIND, dropbearconv_path.resolve(), '/opt/kernel/dropbearconvert')
        _mount(MountTypes.BIND, dropbearkey_path.resolve(), '/opt/kernel/dropbearkey')
        _mount(MountTypes.BIND, tmux_path.resolve(), '/opt/kernel/tmux')
        _mount(MountTypes.BIND, sftp_server_path.resolve(), '/usr/libexec/sftp-server')
        _mount(MountTypes.BIND, scp_path.resolve(), '/usr/bin/scp')

        _mount(MountTypes.VOLUME, krunner_volume, '/opt/backend.ai')
        pylib_path = f'/opt/backend.ai/lib/python{krunner_pyver}/site-packages/'
        _mount(MountTypes.BIND, kernel_pkg_path.resolve(),
                                pylib_path + 'ai/backend/kernel')
        _mount(MountTypes.BIND, helpers_pkg_path.resolve(),
                                pylib_path + 'ai/backend/helpers')
        environ['LD_PRELOAD'] = '/opt/kernel/libbaihook.so'

        # Inject ComputeDevice-specific env-varibles and hooks
        already_injected_hooks: Set[Path] = set()
        for dev_type, device_alloc in resource_spec.allocations.items():
            computer_set = self.computers[dev_type]
            await self.create_kernel__apply_accelerator_allocation(
                ctx, computer_set.instance, device_alloc,
            )
            alloc_sum = Decimal(0)
            for dev_id, per_dev_alloc in device_alloc.items():
                alloc_sum += sum(per_dev_alloc.values())
            if alloc_sum > 0:
                hook_paths = await computer_set.instance.get_hooks(matched_distro, arch)
                if hook_paths:
                    log.debug('accelerator {} provides hooks: {}',
                              type(computer_set.instance).__name__,
                              ', '.join(map(str, hook_paths)))
                for hook_path in map(lambda p: Path(p).absolute(), hook_paths):
                    if hook_path in already_injected_hooks:
                        continue
                    container_hook_path = '/opt/kernel/lib{}{}.so'.format(
                        computer_set.instance.key, secrets.token_hex(6),
                    )
                    _mount(MountTypes.BIND, hook_path, container_hook_path, is_unmanaged=True)
                    environ['LD_PRELOAD'] += ':' + container_hook_path
                    already_injected_hooks.add(hook_path)

    async def execute_batch(
        self,
        kernel_id: KernelId,
        startup_command: str,
    ) -> None:
        kernel_obj = self.kernel_registry.get(kernel_id, None)
        if kernel_obj is None:
            log.warning('execute_batch(k:{}): no such kernel', kernel_id)
            return
        log.debug('execute_batch(k:{}): executing {!r}', kernel_id, (startup_command or '')[:60])
        mode: Literal['batch', 'continue'] = 'batch'
        opts = {
            'exec': startup_command,
        }
        try:
            while True:
                try:
                    result = await self.execute(
                        kernel_id,
                        'batch-job',  # a reserved run ID
                        mode,
                        '',
                        opts=opts,
                        flush_timeout=1.0,
                        api_version=3)
                except KeyError:
                    await self.produce_event(
                        'kernel_terminated',
                        str(kernel_id),
                        'self-terminated',
                        None,
                    )
                    break

                if result['status'] == 'finished':
                    if result['exitCode'] == 0:
                        await self.produce_event(
                            'session_success',
                            str(kernel_id),
                            0,
                            'task-done',
                        )
                    else:
                        await self.produce_event(
                            'session_failure',
                            str(kernel_id),
                            result['exitCode'],
                            'task-failed',
                        )
                    break
                if result['status'] == 'exec-timeout':
                    await self.produce_event(
                        'session_failure',
                        str(kernel_id),
                        -2,
                        'task-timeout',
                    )
                    break
                opts = {
                    'exec': '',
                }
                mode = 'continue'
        except asyncio.CancelledError:
            await self.produce_event(
                'session_failure',
                str(kernel_id),
                -2,
                'task-cancelled',
            )

    async def create_kernel(
        self,
        session_id: SessionId,
        kernel_id: KernelId,
        kernel_config: KernelCreationConfig,
        cluster_info: ClusterInfo,
        *,
        restarting: bool = False,
    ) -> KernelCreationResult:
        """
        Create a new kernel.
        """

        if not restarting:
            await self.produce_event('kernel_preparing', str(kernel_id))

        # Initialize the creation context
        log.debug('Kernel creation config: {0}', pretty(kernel_config))
        ctx = await self.create_kernel__init_context(
            kernel_id, kernel_config,
            restarting=restarting,
        )
        environ: MutableMapping[str, str] = {**kernel_config['environ']}

        # Inject Backend.AI-intrinsic env-variables for gosu
        if KernelFeatures.UID_MATCH in ctx.kernel_features:
            uid = self.local_config['container']['kernel-uid']
            gid = self.local_config['container']['kernel-gid']
            environ['LOCAL_USER_ID'] = str(uid)
            environ['LOCAL_GROUP_ID'] = str(gid)
        environ.update(
            await self.create_kernel__get_extra_envs(ctx)
        )
        image_labels = kernel_config['image']['labels']
        log.debug('image labels:\n{}', pretty(image_labels))

        # Check if we need to pull the container image
        do_pull = await self.check_image(
            ctx.image_ref,
            kernel_config['image']['digest'],
            AutoPullBehavior(kernel_config.get('auto_pull', 'digest')),
        )
        if do_pull:
            await self.produce_event('kernel_pulling',
                                     str(kernel_id), ctx.image_ref.canonical)
            await self.pull_image(ctx.image_ref, kernel_config['image']['registry'])

        if not restarting:
            await self.produce_event('kernel_creating', str(kernel_id))

        # Get the resource spec from existing kernel scratches
        # or create a new resource spec from ctx.kernel_config
        resource_spec, resource_opts = await self.create_kernel__prepare_resource_spec(ctx)
        # When creating a new kernel,
        # we need to allocate agent resources, prepare the networks,
        # adn specify the container mounts.

        # Mount backend-specific intrinsic mounts (e.g., scratch directories)
        resource_spec.mounts.extend(
            await self.create_kernel__get_intrinsic_mounts(ctx)
        )

        # Realize ComputeDevice (including accelerators) allocations.
        slots = resource_spec.slots
        dev_names: Set[DeviceName] = set()
        for slot_name in slots.keys():
            dev_name = slot_name.split('.', maxsplit=1)[0]
            dev_names.add(DeviceName(dev_name))

        if not restarting:
            async with self.resource_lock:
                try:
                    for dev_name in dev_names:
                        computer_set = self.computers[dev_name]
                        device_specific_slots = {
                            SlotName(slot_name): Decimal(alloc)
                            for slot_name, alloc in slots.items()
                            if slot_name.startswith(dev_name)
                        }
                        # TODO: support allocate_evenly()
                        resource_spec.allocations[dev_name] = \
                            computer_set.alloc_map.allocate(
                                device_specific_slots,
                                context_tag=dev_name)
                except InsufficientResource:
                    log.info('insufficient resource: {} of {}\n'
                             '(alloc map: {})',
                             device_specific_slots, dev_name,
                             dict(computer_set.alloc_map.allocations))
                    raise

        # Prepare scratch spaces and dotfiles inside it.
        await self.create_kernel__prepare_scratch(ctx)

        # Prepare networking.
        await self.create_kernel__apply_network(ctx, cluster_info)
        await self.create_kernel__install_ssh_keypair(ctx, cluster_info)

        # Mount vfolders and krunner stuffs.
        await self._create_kernel__mount_vfolders(ctx, kernel_config['mounts'], resource_spec)
        await self._create_kernel__mount_krunner(ctx, resource_spec, environ)

        # Inject Backend.AI-intrinsic env-variables for libbaihook and gosu
        label_envs_corecount = image_labels.get('ai.backend.envs.corecount', '')
        envs_corecount = label_envs_corecount.split(',') if label_envs_corecount else []
        cpu_core_count = len(resource_spec.allocations[DeviceName('cpu')][SlotName('cpu')])
        environ.update({k: str(cpu_core_count) for k in envs_corecount})

        # Realize mounts.
        await self.create_kernel__process_mounts(
            ctx,
            resource_spec.mounts,
        )

        # Get attached devices information (including model_name).
        attached_devices = {}
        for dev_name, device_alloc in resource_spec.allocations.items():
            computer_set = self.computers[dev_name]
            devices = await computer_set.instance.get_attached_devices(device_alloc)
            attached_devices[dev_name] = devices

        exposed_ports = [2000, 2001]
        service_ports = []
        port_map = {}
        preopen_ports = ctx.kernel_config.get('preopen_ports')
        if preopen_ports is None:
            preopen_ports = []

        if ctx.kernel_config['cluster_role'] in ('main', 'master'):
            for sport in parse_service_ports(image_labels.get('ai.backend.service-ports', '')):
                port_map[sport['name']] = sport
            port_map['sshd'] = {
                'name': 'sshd',
                'protocol': ServicePortProtocols('tcp'),
                'container_ports': (2200,),
                'host_ports': (None,),
            }
            port_map['ttyd'] = {
                'name': 'ttyd',
                'protocol': ServicePortProtocols('http'),
                'container_ports': (7681,),
                'host_ports': (None,),
            }
            for port_no in preopen_ports:
                sport = {
                    'name': str(port_no),
                    'protocol': ServicePortProtocols('preopen'),
                    'container_ports': (port_no,),
                    'host_ports': (None,),
                }
                service_ports.append(sport)
                for cport in sport['container_ports']:
                    exposed_ports.append(cport)
            for sport in port_map.values():
                service_ports.append(sport)
                for cport in sport['container_ports']:
                    exposed_ports.append(cport)
            log.debug('exposed ports: {!r}', exposed_ports)

        runtime_type = image_labels.get('ai.backend.runtime-type', 'python')
        runtime_path = image_labels.get('ai.backend.runtime-path', None)
        cmdargs: List[str] = []
        if self.local_config['container']['sandbox-type'] == 'jail':
            cmdargs += [
                "/opt/kernel/jail",
                "-policy", "/etc/backend.ai/jail/policy.yml",
            ]
            if self.local_config['container']['jail-args']:
                cmdargs += map(lambda s: s.strip(), self.local_config['container']['jail-args'])
        cmdargs += [
            "/opt/backend.ai/bin/python",
            "-m", "ai.backend.kernel", runtime_type,
        ]
        if runtime_path is not None:
            cmdargs.append(runtime_path)

        # Store information required for restarts.
        # NOTE: kconfig may be updated after restarts.
        resource_spec.freeze()
        await self.restart_kernel__store_config(
            kernel_id, 'kconfig.dat',
            pickle.dumps(ctx.kernel_config),
        )
        if not restarting:
            await self.restart_kernel__store_config(
                kernel_id, 'cluster.json',
                json.dumps(cluster_info).encode('utf8'),
            )

        log.info('kernel starting with resource spec: \n{0}',
                 pretty(attr.asdict(resource_spec)))
        kernel_obj = await self.create_kernel__spawn(
            ctx,
            resource_spec,
            resource_opts,
            environ,
            service_ports,
            preopen_ports,
            cmdargs,
        )
        self.kernel_registry[ctx.kernel_id] = kernel_obj
        log.debug('kernel repl-in address: {0}:{1}',
                  kernel_obj['kernel_host'], kernel_obj['repl_in_port'])
        log.debug('kernel repl-out address: {0}:{1}',
                  kernel_obj['kernel_host'], kernel_obj['repl_out_port'])

        # Wait until bootstrap script is executed.
        # - Main kernel runner is executed after bootstrap script, and
        #   check_status is accessible only after kernel runner is loaded.
        await kernel_obj.check_status()

        # Update the service-ports metadata from the image labels
        # with the extended template metadata from the agent and krunner.
        live_services = await kernel_obj.get_service_apps()
        if live_services['status'] != 'failed':
            for live_service in live_services['data']:
                for service_port in service_ports:
                    if live_service['name'] == service_port['name']:
                        service_port.update(live_service)
                        break
        log.debug('service ports:\n{!r}', pretty(service_ports))

        # Finally we are done.
        # The kerne_started event is generated by the manager when this RPC call
        # returns the control to the manager, to keep the database up-to-date.

        # The startup command for the batch-type sessions will be executed by the manager
        # upon firing of the "session_started" event.

        return {
            'id': KernelId(kernel_id),
            'kernel_host': str(kernel_obj['kernel_host']),
            'repl_in_port': kernel_obj['repl_in_port'],
            'repl_out_port': kernel_obj['repl_out_port'],
            'stdin_port': kernel_obj['stdin_port'],     # legacy
            'stdout_port': kernel_obj['stdout_port'],   # legacy
            'service_ports': service_ports,
            'container_id': kernel_obj['container_id'],
            'resource_spec': resource_spec.to_json_serializable_dict(),
            'attached_devices': attached_devices,
        }

    @abstractmethod
    async def destroy_kernel(
        self,
        kernel_id: KernelId,
        container_id: Optional[ContainerId],
    ) -> None:
        """
        Initiate destruction of the kernel.

        Things to do:
        * Send SIGTERM to the kernel's main process.
        * Send SIGKILL if it's not terminated within a few seconds.
        """

    @abstractmethod
    async def clean_kernel(
        self,
        kernel_id: KernelId,
        container_id: Optional[ContainerId],
        restarting: bool,
    ) -> None:
        """
        Clean up kernel-related book-keepers when the underlying
        implementation detects an event that the kernel has terminated.

        Things to do:
        * Call :meth:`self.collect_logs()` to store the container's console outputs.
        * Delete the underlying kernel resource (e.g., container)
        * Release host-specific resources used for the kernel (e.g., scratch spaces)

        This method is intended to be called asynchronously by the implementation-specific
        event monitoring routine.

        The ``container_id`` may be ``None`` if the container has already gone away.
        In such cases, skip container-specific cleanups.
        """

    @abstractmethod
    async def create_overlay_network(self, network_name: str) -> None:
        """
        Create an overlay network for a multi-node multicontainer session, where containers in different
        agents can connect to each other using cluster hostnames without explicit port mapping.

        This is called by the manager before kernel creation.
        It may raise :exc:`NotImplementedError` and then the manager
        will cancel creation of the session.
        """

    @abstractmethod
    async def destroy_overlay_network(self, network_name: str) -> None:
        """
        Destroy an overlay network.

        This is called by the manager after kernel destruction.
        """

    @abstractmethod
    async def create_local_network(self, network_name: str) -> None:
        """
        Create a local bridge network for a single-node multicontainer session, where containers in the
        same agent can connect to each other using cluster hostnames without explicit port mapping.
        Depending on the backend, this may be an alias to :meth:`create_overlay_network()`.

        This is called by the manager before kernel creation.
        It may raise :exc:`NotImplementedError` and then the manager
        will cancel creation of the session.
        """

    @abstractmethod
    async def destroy_local_network(self, network_name: str) -> None:
        """
        Destroy a local bridge network.
        Depending on the backend, this may be an alias to :meth:`destroy_overlay_network()`.

        This is called by the manager after kernel destruction.
        """

    @abstractmethod
    async def restart_kernel__load_config(
        self,
        kernel_id: KernelId,
        name: str,
    ) -> bytes:
        """
        Restore the cluster config from a previous launch of the kernel.
        """
        pass

    @abstractmethod
    async def restart_kernel__store_config(
        self,
        kernel_id: KernelId,
        name: str,
        data: bytes,
    ) -> None:
        """
        Store the cluster config to a kernel-related storage (e.g., scratch space),
        so that restarts of this kernel can reuse the configuration.
        """
        pass

    async def restart_kernel(
        self,
        session_id: SessionId,
        kernel_id: KernelId,
        updating_kernel_config: KernelCreationConfig,
    ):
        tracker = self.restarting_kernels.get(kernel_id)
        if tracker is None:
            tracker = RestartTracker(
                request_lock=asyncio.Lock(),
                destroy_event=asyncio.Event(),
                done_event=asyncio.Event())
            self.restarting_kernels[kernel_id] = tracker

        existing_kernel_config = pickle.loads(
            await self.restart_kernel__load_config(kernel_id, 'kconfig.dat')
        )
        existing_cluster_info = json.loads(
            await self.restart_kernel__load_config(kernel_id, 'cluster.json')
        )
        kernel_config = cast(
            KernelCreationConfig,
            {**existing_kernel_config, **updating_kernel_config}
        )
        async with tracker.request_lock:
            tracker.done_event.clear()
            await self.inject_container_lifecycle_event(
                kernel_id,
                LifecycleEvent.DESTROY,
                'restarting',
            )
            try:
                with timeout(60):
                    await tracker.destroy_event.wait()
            except asyncio.TimeoutError:
                log.warning('timeout detected while restarting kernel {0}!',
                            kernel_id)
                self.restarting_kernels.pop(kernel_id, None)
                await self.inject_container_lifecycle_event(
                    kernel_id,
                    LifecycleEvent.CLEAN,
                    'restart-timeout',
                )
                raise
            else:
                # tracker.destroy_event.clear()
                try:
                    await self.create_kernel(
                        session_id,
                        kernel_id,
                        kernel_config,
                        existing_cluster_info,
                        restarting=True)
                    self.restarting_kernels.pop(kernel_id, None)
                except Exception:
                    # TODO: retry / cancel others?
                    log.exception('restart_kernel(s:{}, k:{}): re-creation failure',
                                  session_id, kernel_id)
            tracker.done_event.set()
        kernel_obj = self.kernel_registry[kernel_id]
        return {
            'container_id': kernel_obj['container_id'],
            'repl_in_port': kernel_obj['repl_in_port'],
            'repl_out_port': kernel_obj['repl_out_port'],
            'stdin_port': kernel_obj['stdin_port'],
            'stdout_port': kernel_obj['stdout_port'],
            'service_ports': kernel_obj.service_ports,
        }

    async def execute(
        self,
        kernel_id: KernelId,
        run_id: Optional[str],
        mode: Literal['query', 'batch', 'input', 'continue'],
        text: str,
        *,
        opts: Mapping[str, Any],
        api_version: int,
        flush_timeout: float,
    ):
        # Wait for the kernel restarting if it's ongoing...
        restart_tracker = self.restarting_kernels.get(kernel_id)
        if restart_tracker is not None:
            await restart_tracker.done_event.wait()

        try:
            kernel_obj = self.kernel_registry[kernel_id]
            result = await kernel_obj.execute(
                run_id, mode, text,
                opts=opts,
                flush_timeout=flush_timeout,
                api_version=api_version)
        except KeyError:
            # This situation is handled in the lifecycle management subsystem.
            raise RuntimeError(f'The container for kernel {kernel_id} is not found! '
                                '(might be terminated--try it again)') from None

        if result['status'] in ('finished', 'exec-timeout'):
            log.debug('_execute({0}) {1}', kernel_id, result['status'])
        if result['status'] == 'exec-timeout':
            await self.inject_container_lifecycle_event(
                kernel_id,
                LifecycleEvent.DESTROY,
                'exec-timeout',
            )
        return {
            **result,
            'files': [],  # kept for API backward-compatibility
        }

    async def get_completions(self, kernel_id: KernelId, text: str, opts: dict):
        return await self.kernel_registry[kernel_id].get_completions(text, opts)

    async def get_logs(self, kernel_id: KernelId):
        return await self.kernel_registry[kernel_id].get_logs()

    async def interrupt_kernel(self, kernel_id: KernelId):
        return await self.kernel_registry[kernel_id].interrupt_kernel()

    async def start_service(self, kernel_id: KernelId, service: str, opts: dict):
        return await self.kernel_registry[kernel_id].start_service(service, opts)

    async def shutdown_service(self, kernel_id: KernelId, service: str):
        try:
            kernel_obj = self.kernel_registry[kernel_id]
            if kernel_obj is not None:
                await kernel_obj.shutdown_service(service)
        except Exception:
            log.exception('unhandled exception while shutting down service app ${}', service)

    async def accept_file(self, kernel_id: KernelId, filename: str, filedata):
        return await self.kernel_registry[kernel_id].accept_file(filename, filedata)

    async def download_file(self, kernel_id: KernelId, filepath: str):
        return await self.kernel_registry[kernel_id].download_file(filepath)

    async def list_files(self, kernel_id: KernelId, path: str):
        return await self.kernel_registry[kernel_id].list_files(path)
