################################################################################
# Copyright (c) 2013-2024, National Research Foundation (SARAO)
#
# Licensed under the BSD 3-Clause License (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy
# of the License at
#
#   https://opensource.org/licenses/BSD-3-Clause
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
################################################################################

"""Class for katcp connections that proxies sensors into a server"""

import enum
import functools
import logging
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Type,
    Union,
)

import aiokatcp
import prometheus_client  # noqa: F401
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

logger = logging.getLogger(__name__)
_Metric = Union[Gauge, Counter, Histogram]
# Use a string so that code won't completely break if Prometheus internals change.
# The Union is required because mypy doesn't like pure strings being used indirectly.
_LabelWrapper = Union["prometheus_client.core._LabelWrapper"]
_Factory = Callable[[aiokatcp.Sensor], Optional["PrometheusInfo"]]


def _dummy_factory(sensor: aiokatcp.Sensor) -> None:
    return None


def _reading_to_float(reading: aiokatcp.Reading) -> float:
    value = reading.value
    if isinstance(value, enum.Enum):
        try:
            values: List[enum.Enum] = list(type(value))
            idx = values.index(value)
        except ValueError:
            idx = -1
        return float(idx)
    else:
        return float(reading.value)


class CloseAction(enum.Enum):
    """Action to take on sensors when a :class:`SensorWatcher` is closed."""

    REMOVE = 1  #: Remove the sensors from the server
    UNREACHABLE = 2  #: Change the sensor statuses to unreachable


class _FilterPredicate(Protocol):
    def __call__(
        self, name: str, description: str, units: str, type_name: str, *args: bytes
    ) -> bool:
        ...  # pragma: nocover


class SensorWatcher(aiokatcp.SensorWatcher):
    """Mirrors sensors from a client into a server.

    See :class:`SensorProxyClient` for an explanation of the parameters.
    """

    def __init__(
        self,
        client: aiokatcp.Client,
        server: aiokatcp.DeviceServer,
        prefix: str,
        rewrite_gui_urls: Optional[Callable[[aiokatcp.Sensor], bytes]] = None,
        enum_types: Sequence[Type[enum.Enum]] = (),
        renames: Optional[Mapping[str, Union[str, Sequence[str]]]] = None,
        close_action: CloseAction = CloseAction.REMOVE,
        notify: Optional[Callable[[], Any]] = None,
        filter: Optional[_FilterPredicate] = None,
    ) -> None:
        super().__init__(client, enum_types)
        self.prefix = prefix
        self.renames = renames if renames is not None else {}
        self.server = server
        # We keep track of the sensors after name rewriting but prior to gui-url rewriting
        self.orig_sensors: aiokatcp.SensorSet
        try:
            self.orig_sensors = self.server.orig_sensors  # type: ignore
        except AttributeError:
            self.orig_sensors = aiokatcp.SensorSet()

        self.rewrite_gui_urls = rewrite_gui_urls
        self.close_action = close_action
        if notify is not None:
            self.notify = notify
        else:
            self.notify = functools.partial(server.mass_inform, "interface-changed", "sensor-list")
        # Whether we need to call notify at the end of the batch
        self._need_notify = False
        self._filter = filter

    def filter(self, name: str, description: str, units: str, type_name: str, *args: bytes) -> bool:
        if self._filter is not None:
            return self._filter(name, description, units, type_name, *args)
        return super().filter(name, description, units, type_name, *args)

    def rewrite_name(self, name: str) -> Sequence[str]:
        names = self.renames.get(name, self.prefix + name)
        if isinstance(names, str):
            names = [names]
        return names

    def sensor_added(
        self, name: str, description: str, units: str, type_name: str, *args: bytes
    ) -> None:
        """Add a new or replaced sensor with unqualified name `name`."""
        super().sensor_added(name, description, units, type_name, *args)
        for rewritten_name in self.rewrite_name(name):
            sensor = self.sensors[rewritten_name]
            self.orig_sensors.add(sensor)
            if (
                self.rewrite_gui_urls is not None
                and sensor.name.endswith(".gui-urls")
                and sensor.stype is bytes
            ):
                new_value = self.rewrite_gui_urls(sensor)
                sensor = aiokatcp.Sensor(
                    sensor.stype,
                    sensor.name,
                    sensor.description,
                    sensor.units,
                    new_value,
                    sensor.status,
                )
            self.server.sensors.add(sensor)
        self._need_notify = True

    def _sensor_removed(self, name: str) -> None:
        """Like :meth:`sensor_removed`, but takes the rewritten name"""
        self.server.sensors.pop(name, None)
        self.orig_sensors.pop(name, None)
        self._need_notify = True

    def sensor_removed(self, name: str) -> None:
        super().sensor_removed(name)
        for rewritten_name in self.rewrite_name(name):
            self._sensor_removed(rewritten_name)

    def sensor_updated(
        self, name: str, value: bytes, status: aiokatcp.Sensor.Status, timestamp: float
    ) -> None:
        super().sensor_updated(name, value, status, timestamp)
        for rewritten_name in self.rewrite_name(name):
            sensor = self.sensors[rewritten_name]
            if (
                self.rewrite_gui_urls is not None
                and rewritten_name.endswith(".gui-urls")
                and sensor.stype is bytes
            ):
                value = self.rewrite_gui_urls(sensor)
                self.server.sensors[rewritten_name].set_value(value, status, timestamp)

    def batch_stop(self) -> None:
        super().batch_stop()
        if self._need_notify:
            self.notify()
        self._need_notify = False

    def _mark_unreachable(self, sensor: aiokatcp.Sensor) -> None:
        # Special case for device-status sensors: if we can no longer
        # communicate with the device, treat it as failed.
        if sensor.name in self.rewrite_name("device-status") and sensor.type_name == "discrete":
            try:
                fail_value = sensor.stype(b"fail")
            except ValueError:
                pass
            else:
                sensor.set_value(fail_value, status=aiokatcp.Sensor.Status.ERROR)
                return
        reading = sensor.reading
        if reading.status != aiokatcp.Sensor.Status.UNREACHABLE:
            # We could keep the last value, but that could be a large string and
            # we don't want to spam clients with that (particularly since we're
            # updating all the sensors at once).
            default_value = aiokatcp.core.get_type(sensor.stype).default(sensor.stype)
            sensor.set_value(default_value, status=aiokatcp.Sensor.Status.UNREACHABLE)

    def state_updated(self, state: aiokatcp.SyncState) -> None:
        super().state_updated(state)
        if state == aiokatcp.SyncState.CLOSED:
            self.batch_start()
            for name in self.sensors.keys():
                if self.close_action == CloseAction.UNREACHABLE:
                    self._mark_unreachable(self.server.sensors[name])
                    self._mark_unreachable(self.orig_sensors[name])
                elif self.close_action == CloseAction.REMOVE:
                    self._sensor_removed(name)
            self.batch_stop()


class SensorProxyClient(aiokatcp.Client):
    """Client that mirrors sensors into a device server.

    Parameters
    ----------
    server
        Server to which sensors will be added
    prefix
        String prepended to the remote server's sensor names to obtain names
        used on `server`. These should be unique per `server` to avoid
        collisions.
    rewrite_gui_urls
        If given, a function that is given a ``.gui-urls`` sensor and returns a
        replacement value. Note that the function is responsible for decoding
        and encoding between JSON and :class:`bytes`.
    renames
        Mapping from the remote server's sensor names to sensor names for
        `server`. Sensors found in this mapping do not have `prefix` applied.
        The values may also be lists of strings, in which case the sensor
        will be duplicated under each of these names.
    close_action
        Defines what to do with the sensors when the connection is closed.
    notify
        Callback which is called when there are changes to the sensor list.
        If not specified, it defaults to sending an ``interface-changed``
        inform to all clients of the server.
    filter
        Predicate used to decide which sensors are required. The default is
        to use all of them. See :meth:`aiokatcp.AbstractSensorWatcher.filter`.
    kwargs
        Passed to the base class
    """

    def __init__(
        self,
        server: aiokatcp.DeviceServer,
        prefix: str,
        rewrite_gui_urls: Optional[Callable[[aiokatcp.Sensor], bytes]] = None,
        enum_types: Sequence[Type[enum.Enum]] = (),
        renames: Optional[Mapping[str, Union[str, Sequence[str]]]] = None,
        close_action: CloseAction = CloseAction.REMOVE,
        notify: Optional[Callable[[], Any]] = None,
        filter: Optional[_FilterPredicate] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        watcher = SensorWatcher(
            self,
            server,
            prefix,
            rewrite_gui_urls,
            renames=renames,
            close_action=close_action,
            notify=notify,
            filter=filter,
        )
        self._synced = watcher.synced
        self.add_sensor_watcher(watcher)

    async def wait_synced(self) -> None:
        await self._synced.wait()


class PrometheusInfo:
    """Specify information for creating a Prometheus series from a katcp sensor.

    Parameters
    ----------
    class_
        Callable that produces the Prometheus metric from `name`, `description` and labels
    name
        Name of the Prometheus metric
    description
        Description for the Prometheus metric
    labels
        Labels to combine with `name` to produce the series.
    """

    def __init__(
        self,
        class_: Callable[..., _LabelWrapper],
        name: str,
        description: str,
        labels: Mapping[str, str],
        registry: CollectorRegistry = prometheus_client.REGISTRY,
    ) -> None:
        self.class_ = class_
        self.name = name
        self.description = description
        self.labels = dict(labels)
        self.registry = registry


class PrometheusObserver:
    """Watches a sensor and mirrors updates into a Prometheus Gauge, Counter or Histogram."""

    def __init__(
        self,
        sensor: aiokatcp.Sensor,
        value_metric: _LabelWrapper,
        status_metric: _LabelWrapper,
        label_values: Iterable[str],
    ) -> None:
        self._sensor = sensor
        self._old_value = 0.0
        self._old_timestamp: Optional[float] = None
        self._value_metric_root = value_metric
        self._status_metric_root = status_metric
        self._value_metric: _Metric = value_metric.labels(*label_values)
        self._status_metric: _Metric = status_metric.labels(*label_values)
        self._label_values = tuple(label_values)
        sensor.attach(self)

    def __call__(self, sensor: aiokatcp.Sensor, reading: aiokatcp.Reading) -> None:
        valid = reading.status.valid_value()
        value = _reading_to_float(reading) if valid else 0.0
        timestamp = reading.timestamp
        self._status_metric.set(reading.status.value)
        # Detecting the type of the metric is tricky, because Counter and
        # Gauge aren't actually classes (they're functions). So we have to
        # use introspection.
        metric_type = type(self._value_metric).__name__
        if metric_type == "Gauge":
            if valid:
                self._value_metric.set(value)
        elif metric_type == "Counter":
            # If the sensor is invalid, then the counter isn't increasing
            if valid:
                if value < self._old_value:
                    logger.debug(
                        "Counter %s went backwards (%d to %d), not sending delta to Prometheus",
                        self._sensor.name,
                        self._old_value,
                        value,
                    )
                    # self._old_value is still updated with value. This is
                    # appropriate if the counter was reset (e.g. at the
                    # start of a new observation), so that Prometheus
                    # sees a cumulative count.
                else:
                    self._value_metric.inc(value - self._old_value)
        elif metric_type == "Histogram":
            if valid and timestamp != self._old_timestamp:
                self._value_metric.observe(value)
        else:
            raise TypeError(f"Expected a Counter, Gauge or Histogram, not {metric_type}")
        if valid:
            self._old_value = value
        self._old_timestamp = timestamp

    def close(self) -> None:
        """Shut down observing"""
        self._sensor.detach(self)
        self._status_metric_root.remove(*self._label_values)
        self._value_metric_root.remove(*self._label_values)


class PrometheusWatcher:
    """Mirror sensors from a :class:`aiokatcp.SensorSet` into Prometheus metrics.

    It automatically deals with additions and removals of sensors from the
    sensor set.

    Parameters
    ----------
    sensors
        Sensors to monitor.
    labels
        Extra labels to apply to every sensor.
    factory
        Extracts information to create the Prometheus series from a sensor.
        It may return ``None`` to skip generating a Prometheus series for that
        sensor. This argument may also be ``None`` to skip creating any
        Prometheus metrics.
    metrics
        Store for Prometheus metrics. If not provided, generated sensors are
        stored in a class-level variable. This is mainly intended to allow
        tests to be isolated from global state.
    """

    # Caches metrics by name. Each entry stores the primary metric
    # and the status metric.
    _metrics: Dict[str, Tuple[_LabelWrapper, _LabelWrapper]] = {}

    def __init__(
        self,
        sensors: aiokatcp.SensorSet,
        labels: Optional[Mapping[str, str]] = None,
        factory: Optional[_Factory] = None,
        metrics: Optional[Dict[str, Tuple[_LabelWrapper, _LabelWrapper]]] = None,
    ) -> None:
        self.sensors = sensors
        # Indexed by sensor name; None if no observer is needed.
        # Invariant: _observers has an entry if and only if the corresponding
        # sensor exists in self.sensors (except after `close`).
        self._observers: Dict[str, Optional[PrometheusObserver]] = {}
        if labels is not None:
            self._labels = labels
        else:
            self._labels = {}
        if factory is not None:
            self._factory = factory
        else:
            self._factory = _dummy_factory
        if metrics is not None:
            self._metrics = metrics
        for sensor in self.sensors.values():
            self._added(sensor)
        self.sensors.add_add_callback(self._added)
        self.sensors.add_remove_callback(self._removed)

    def _make_observer(self, sensor: aiokatcp.Sensor) -> Optional[PrometheusObserver]:
        """Make a :class:`PrometheusObserver` for sensor `sensor`, if appropriate.

        Otherwise returns ``None``.
        """
        info = self._factory(sensor)
        if info is None:
            return None
        try:
            value_metric, status_metric = self._metrics[info.name]
        except KeyError:
            label_names = list(self._labels.keys()) + list(info.labels.keys())
            value_metric = info.class_(
                info.name, info.description, label_names, registry=info.registry
            )
            status_metric = Gauge(
                info.name + "_status",
                f"Status of katcp sensor {info.name}",
                label_names,
                registry=info.registry,
            )
            self._metrics[info.name] = (value_metric, status_metric)

        label_values = list(self._labels.values()) + list(info.labels.values())
        observer = PrometheusObserver(sensor, value_metric, status_metric, label_values)
        # Populate initial value
        observer(sensor, sensor.reading)
        return observer

    def _added(self, sensor: aiokatcp.Sensor) -> None:
        old_observer = self._observers.get(sensor.name)
        if old_observer is not None:
            old_observer.close()
        self._observers[sensor.name] = self._make_observer(sensor)

    def _removed(self, sensor: aiokatcp.Sensor) -> None:
        old_observer = self._observers.pop(sensor.name, None)
        if old_observer is not None:
            old_observer.close()

    def close(self) -> None:
        for observer in self._observers.values():
            if observer is not None:
                observer.close()
        self._observers = {}
        try:
            self.sensors.remove_remove_callback(self._removed)
        except ValueError:
            pass
        try:
            self.sensors.remove_add_callback(self._added)
        except ValueError:
            pass
