"""The Tuya BLE integration."""
from __future__ import annotations

from dataclasses import dataclass, field

import logging
from typing import Any, Callable

from homeassistant.components.number import (
    NumberEntityDescription,
    NumberEntity,
    RestoreNumber,
)
from homeassistant.components.number.const import NumberDeviceClass, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    UnitOfVolume,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .devices import TuyaBLEData, TuyaBLEEntity, TuyaBLEProductInfo
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)

TuyaBLENumberGetter = (
    Callable[["TuyaBLENumber", TuyaBLEProductInfo], float | None] | None
)


TuyaBLENumberIsAvailable = (
    Callable[["TuyaBLENumber", TuyaBLEProductInfo], bool] | None
)


TuyaBLENumberSetter = (
    Callable[["TuyaBLENumber", TuyaBLEProductInfo, float], None] | None
)


@dataclass
class TuyaBLENumberMapping:
    dp_id: int
    description: NumberEntityDescription
    force_add: bool = True
    dp_type: TuyaBLEDataPointType | None = None
    coefficient: float = 1.0
    is_available: TuyaBLENumberIsAvailable = None
    getter: TuyaBLENumberGetter = None
    setter: TuyaBLENumberSetter = None
    mode: NumberMode = NumberMode.BOX


def is_fingerbot_in_program_mode(
    self: TuyaBLENumber,
    product: TuyaBLEProductInfo,
) -> bool:
    result: bool = True
    if product.fingerbot:
        datapoint = self._device.datapoints[product.fingerbot.mode]
        if datapoint:
            result = datapoint.value == 2
    return result


def is_fingerbot_not_in_program_mode(
    self: TuyaBLENumber,
    product: TuyaBLEProductInfo,
) -> bool:
    result: bool = True
    if product.fingerbot:
        datapoint = self._device.datapoints[product.fingerbot.mode]
        if datapoint:
            result = datapoint.value != 2
    return result


def is_fingerbot_in_push_mode(
    self: TuyaBLENumber,
    product: TuyaBLEProductInfo,
) -> bool:
    result: bool = True
    if product.fingerbot:
        datapoint = self._device.datapoints[product.fingerbot.mode]
        if datapoint:
            result = datapoint.value == 0
    return result


def is_fingerbot_repeat_count_available(
    self: TuyaBLENumber,
    product: TuyaBLEProductInfo,
) -> bool:
    result: bool = True
    if product.fingerbot and product.fingerbot.program:
        datapoint = self._device.datapoints[product.fingerbot.mode]
        if datapoint:
            result = datapoint.value == 2
        if result:
            datapoint = self._device.datapoints[product.fingerbot.program]
            if datapoint and type(datapoint.value) is bytes:
                repeat_count = int.from_bytes(datapoint.value[0:2], "big")
                result = repeat_count != 0xFFFF

    return result


def get_fingerbot_program_repeat_count(
    self: TuyaBLENumber,
    product: TuyaBLEProductInfo,
) -> float | None:
    result: float | None = None
    if product.fingerbot and product.fingerbot.program:
        datapoint = self._device.datapoints[product.fingerbot.program]
        if datapoint and type(datapoint.value) is bytes:
            repeat_count = int.from_bytes(datapoint.value[0:2], "big")
            result = repeat_count * 1.0

    return result


def set_fingerbot_program_repeat_count(
    self: TuyaBLENumber,
    product: TuyaBLEProductInfo,
    value: float,
) -> None:
    if product.fingerbot and product.fingerbot.program:
        datapoint = self._device.datapoints[product.fingerbot.program]
        if datapoint and type(datapoint.value) is bytes:
            new_value = (
                int.to_bytes(int(value), 2, "big") +
                datapoint.value[2:]
            )
            self._hass.create_task(datapoint.set_value(new_value))


def get_fingerbot_program_position(
    self: TuyaBLENumber,
    product: TuyaBLEProductInfo,
) -> float | None:
    result: float | None = None
    if product.fingerbot and product.fingerbot.program:
        datapoint = self._device.datapoints[product.fingerbot.program]
        if datapoint and type(datapoint.value) is bytes:
            result = datapoint.value[2] * 1.0

    return result


def set_fingerbot_program_position(
    self: TuyaBLENumber,
    product: TuyaBLEProductInfo,
    value: float,
) -> None:
    if product.fingerbot and product.fingerbot.program:
        datapoint = self._device.datapoints[product.fingerbot.program]
        if datapoint and type(datapoint.value) is bytes:
            new_value = bytearray(datapoint.value)
            new_value[2] = int(value)
            self._hass.create_task(datapoint.set_value(new_value))


@dataclass
class TuyaBLEDownPositionDescription(NumberEntityDescription):
    key: str = "down_position"
    icon: str = "mdi:arrow-down-bold"
    native_max_value: float = 100
    native_min_value: float = 51
    native_unit_of_measurement: str = PERCENTAGE
    native_step: float = 1
    entity_category: EntityCategory = EntityCategory.CONFIG


@dataclass
class TuyaBLEUpPositionDescription(NumberEntityDescription):
    key: str = "up_position"
    icon: str = "mdi:arrow-up-bold"
    native_max_value: float = 50
    native_min_value: float = 0
    native_unit_of_measurement: str = PERCENTAGE
    native_step: float = 1
    entity_category: EntityCategory = EntityCategory.CONFIG


@dataclass
class TuyaBLEHoldTimeDescription(NumberEntityDescription):
    key: str = "hold_time"
    icon: str = "mdi:timer"
    native_max_value: float = 10
    native_min_value: float = 0
    native_unit_of_measurement: str = UnitOfTime.SECONDS
    native_step: float = 1
    entity_category: EntityCategory = EntityCategory.CONFIG


@dataclass
class TuyaBLEHoldTimeMapping(TuyaBLENumberMapping):
    description: NumberEntityDescription = field(
        default_factory=lambda: TuyaBLEHoldTimeDescription()
    )
    is_available: TuyaBLENumberIsAvailable = is_fingerbot_in_push_mode


@dataclass
class TuyaBLECategoryNumberMapping:
    products: dict[str, list[TuyaBLENumberMapping]] | None = None
    mapping: list[TuyaBLENumberMapping] | None = None


# Special class for virtual entities that don't correspond to actual datapoints
@dataclass
class TuyaBLEVirtualNumberMapping:
    description: NumberEntityDescription
    force_add: bool = True
    is_available: TuyaBLENumberIsAvailable = None
    getter: TuyaBLENumberGetter = None
    setter: TuyaBLENumberSetter = None
    mode: NumberMode = NumberMode.BOX
    default_value: float = 3600  # Default to 1 hour


mapping: dict[str, TuyaBLECategoryNumberMapping] = {
    "sfkzq": TuyaBLECategoryNumberMapping(
        products={
            "nxquc5lb":  # Smart Water Valve
            [
                TuyaBLENumberMapping(
                    dp_id=9,
                    description=NumberEntityDescription(
                        key="time_use",
                        icon="mdi:timer",
                        native_max_value=2592000,
                        native_min_value=0,
                        native_unit_of_measurement=UnitOfTime.SECONDS,
                        native_step=1,
                        entity_category=EntityCategory.CONFIG,
                    ),
                ),
                TuyaBLENumberMapping(
                    dp_id=11,
                    description=NumberEntityDescription(
                        key="countdown",
                        icon="mdi:timer-outline",
                        native_max_value=86400,
                        native_min_value=0,
                        native_unit_of_measurement=UnitOfTime.SECONDS,
                        native_step=1,
                        entity_category=EntityCategory.CONFIG,
                    ),
                ),
                # Add a virtual entity for watering duration setting
                TuyaBLEVirtualNumberMapping(
                    description=NumberEntityDescription(
                        key="watering_duration",
                        name="Watering Duration",
                        icon="mdi:water-timer",
                        native_max_value=86400,  # 30 days in seconds
                        native_min_value=60,       # Minimum 1 minute
                        native_unit_of_measurement=UnitOfTime.SECONDS,
                        native_step=60,            # 1 minute steps
                        entity_category=EntityCategory.CONFIG,
                    ),
                    default_value=900,  # Default to 15 minutes
                ),
            ]
        }
    ),
    "co2bj": TuyaBLECategoryNumberMapping(
        products={
            "59s19z5m": [  # CO2 Detector
                TuyaBLENumberMapping(
                    dp_id=17,
                    description=NumberEntityDescription(
                        key="brightness",
                        icon="mdi:brightness-percent",
                        native_max_value=100,
                        native_min_value=0,
                        native_unit_of_measurement=PERCENTAGE,
                        native_step=1,
                        entity_category=EntityCategory.CONFIG,
                    ),
                    mode=NumberMode.SLIDER,
                ),
                TuyaBLENumberMapping(
                    dp_id=26,
                    description=NumberEntityDescription(
                        key="carbon_dioxide_alarm_level",
                        icon="mdi:molecule-co2",
                        native_max_value=5000,
                        native_min_value=400,
                        native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
                        native_step=100,
                        entity_category=EntityCategory.CONFIG,
                    ),
                ),
            ],
        },
    ),
    "szjqr": TuyaBLECategoryNumberMapping(
        products={
            **dict.fromkeys(
                ["3yqdo5yt", "xhf790if"],  # CubeTouch 1s and II
                [
                    TuyaBLEHoldTimeMapping(dp_id=3),
                    TuyaBLENumberMapping(
                        dp_id=5,
                        description=TuyaBLEUpPositionDescription(
                            native_max_value=100,
                        ),
                    ),
                    TuyaBLENumberMapping(
                        dp_id=6,
                        description=TuyaBLEDownPositionDescription(
                            native_min_value=0,
                        ),
                    ),
                ],
            ),
            **dict.fromkeys(
                [
                    "blliqpsj",
                    "ndvkgsrm",
                    "yiihr7zh",
                    "neq16kgd"
                ],  # Fingerbot Plus
                [
                    TuyaBLENumberMapping(
                        dp_id=9,
                        description=TuyaBLEDownPositionDescription(),
                        is_available=is_fingerbot_not_in_program_mode,
                    ),
                    TuyaBLEHoldTimeMapping(dp_id=10),
                    TuyaBLENumberMapping(
                        dp_id=15,
                        description=TuyaBLEUpPositionDescription(),
                        is_available=is_fingerbot_not_in_program_mode,
                    ),
                    TuyaBLENumberMapping(
                        dp_id=121,
                        description=NumberEntityDescription(
                            key="program_repeats_count",
                            icon="mdi:repeat",
                            native_max_value=0xFFFE,
                            native_min_value=1,
                            native_step=1,
                            entity_category=EntityCategory.CONFIG,
                        ),
                        is_available=is_fingerbot_repeat_count_available,
                        getter=get_fingerbot_program_repeat_count,
                        setter=set_fingerbot_program_repeat_count,
                    ),
                    TuyaBLENumberMapping(
                        dp_id=121,
                        description=NumberEntityDescription(
                            key="program_idle_position",
                            icon="mdi:repeat",
                            native_max_value=100,
                            native_min_value=0,
                            native_step=1,
                            native_unit_of_measurement=PERCENTAGE,
                            entity_category=EntityCategory.CONFIG,
                        ),
                        is_available=is_fingerbot_in_program_mode,
                        getter=get_fingerbot_program_position,
                        setter=set_fingerbot_program_position,
                    ),
                ],
            ),
            **dict.fromkeys(
                [
                    "ltak7e1p",
                    "y6kttvd6",
                    "yrnk7mnn",
                    "nvr2rocq",
                    "bnt7wajf",
                    "rvdceqjh",
                    "5xhbk964",
                ],  # Fingerbot
                [
                    TuyaBLENumberMapping(
                        dp_id=9,
                        description=TuyaBLEDownPositionDescription(),
                        is_available=is_fingerbot_not_in_program_mode,
                    ),
                    TuyaBLENumberMapping(
                        dp_id=10,
                        description=TuyaBLEHoldTimeDescription(
                            native_step=0.1,
                        ),
                        coefficient=10.0,
                        is_available=is_fingerbot_in_push_mode,
                    ),
                    TuyaBLENumberMapping(
                        dp_id=15,
                        description=TuyaBLEUpPositionDescription(),
                        is_available=is_fingerbot_not_in_program_mode,
                    ),
                ],
            ),
        },
    ),
    "kg": TuyaBLECategoryNumberMapping(
        products={
            **dict.fromkeys(
                [
                    "mknd4lci",
                    "riecov42"
                ],  # Fingerbot Plus
                [
                    TuyaBLENumberMapping(
                        dp_id=102,
                        description=TuyaBLEDownPositionDescription(),
                        is_available=is_fingerbot_not_in_program_mode,
                    ),
                    TuyaBLEHoldTimeMapping(dp_id=103),
                    TuyaBLENumberMapping(
                        dp_id=106,
                        description=TuyaBLEUpPositionDescription(),
                        is_available=is_fingerbot_not_in_program_mode,
                    ),
                    TuyaBLENumberMapping(
                        dp_id=109,
                        description=NumberEntityDescription(
                            key="program_repeats_count",
                            icon="mdi:repeat",
                            native_max_value=0xFFFE,
                            native_min_value=1,
                            native_step=1,
                            entity_category=EntityCategory.CONFIG,
                        ),
                        is_available=is_fingerbot_repeat_count_available,
                        getter=get_fingerbot_program_repeat_count,
                        setter=set_fingerbot_program_repeat_count,
                    ),
                    TuyaBLENumberMapping(
                        dp_id=109,
                        description=NumberEntityDescription(
                            key="program_idle_position",
                            icon="mdi:repeat",
                            native_max_value=100,
                            native_min_value=0,
                            native_step=1,
                            native_unit_of_measurement=PERCENTAGE,
                            entity_category=EntityCategory.CONFIG,
                        ),
                        is_available=is_fingerbot_in_program_mode,
                        getter=get_fingerbot_program_position,
                        setter=set_fingerbot_program_position,
                    ),
                ],
            ),
        },
    ),
    "wk": TuyaBLECategoryNumberMapping(
        products={
            **dict.fromkeys(
                [
                    "drlajpqc",
                    "nhj2j7su",
                    "zmachryv",
                ],  # Thermostatic Radiator Valve
                [
                    TuyaBLENumberMapping(
                        dp_id=27,
                        description=NumberEntityDescription(
                            key="temperature_calibration",
                            icon="mdi:thermometer-lines",
                            native_max_value=6,
                            native_min_value=-6,
                            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                            native_step=1,
                            entity_category=EntityCategory.CONFIG,
                        ),
                    ),
                ],
            ),
        },
    ),
    "wsdcg": TuyaBLECategoryNumberMapping(
        products={
            "ojzlzzsw": [  # Soil moisture sensor
                TuyaBLENumberMapping(
                    dp_id=17,
                    description=NumberEntityDescription(
                        key="reporting_period",
                        icon="mdi:timer",
                        native_max_value=120,
                        native_min_value=1,
                        native_unit_of_measurement=UnitOfTime.MINUTES,
                        native_step=1,
                        entity_category=EntityCategory.CONFIG,
                    ),
                ),
            ],
        },
    ),
    "znhsb": TuyaBLECategoryNumberMapping(
        products={
            "cdlandip":  # Smart water bottle
            [
                TuyaBLENumberMapping(
                    dp_id=103,
                    description=NumberEntityDescription(
                        key="recommended_water_intake",
                        device_class=NumberDeviceClass.WATER,
                        native_max_value=5000,
                        native_min_value=0,
                        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
                        native_step=1,
                        entity_category=EntityCategory.CONFIG,
                    ),
                ),
            ],
        },
    ),
    "ggq": TuyaBLECategoryNumberMapping(
        products={
            "6pahkcau": [  # Irrigation computer PARKSIDE PPB A1
                TuyaBLENumberMapping(
                    dp_id=5,
                    description=NumberEntityDescription(
                        key="countdown_duration",
                        icon="mdi:timer",
                        native_max_value=1440,
                        native_min_value=1,
                        native_unit_of_measurement=UnitOfTime.MINUTES,
                        native_step=1,
                    ),
                ),
            ],
            "hfgdqhho": [  # Irrigation computer SGW08
                TuyaBLENumberMapping(
                    dp_id=106,
                    description=NumberEntityDescription(
                        key="countdown_duration_1",
                        name="CH1 Countdown",
                        icon="mdi:timer",
                        native_max_value=1440,
                        native_min_value=1,
                        native_unit_of_measurement=UnitOfTime.MINUTES,
                        native_step=1,
                    ),
                ),
                TuyaBLENumberMapping(
                    dp_id=103,
                    description=NumberEntityDescription(
                        key="countdown_duration_2",
                        name="CH2 Countdown",
                        icon="mdi:timer",
                        native_max_value=1440,
                        native_min_value=1,
                        native_unit_of_measurement=UnitOfTime.MINUTES,
                        native_step=1,
                    ),
                ),
            ],
            "hfgdqhho": [  # Irrigation computer - SGW02
                TuyaBLENumberMapping(
                    dp_id=106,
                    description=NumberEntityDescription(
                        key="countdown_duration_z1",
                        icon="mdi:timer",
                        native_max_value=1440,
                        native_min_value=1,
                        native_unit_of_measurement=UnitOfTime.MINUTES,
                        native_step=1,
                    ),
                ),
                TuyaBLENumberMapping(
                    dp_id=103,
                    description=NumberEntityDescription(
                        key="countdown_duration_z2",
                        icon="mdi:timer",
                        native_max_value=1440,
                        native_min_value=1,
                        native_unit_of_measurement=UnitOfTime.MINUTES,
                        native_step=1,
                    ),
                ),
            ],
        },
    ),
}


def get_mapping_by_device(device: TuyaBLEDevice) -> list[TuyaBLECategoryNumberMapping]:
    category = mapping.get(device.category)
    if category is not None and category.products is not None:
        product_mapping = category.products.get(device.product_id)
        if product_mapping is not None:
            return product_mapping
        if category.mapping is not None:
            return category.mapping
        else:
            return []
    else:
        return []


class TuyaBLENumber(TuyaBLEEntity, NumberEntity):
    """Representation of a Tuya BLE Number."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLENumberMapping,
    ) -> None:
        super().__init__(hass, coordinator, device, product, mapping.description)
        self._mapping = mapping
        self._attr_mode = mapping.mode

    @property
    def native_value(self) -> float | None:
        """Return the entity value to represent the entity state."""
        if self._mapping.getter:
            return self._mapping.getter(self, self._product)

        datapoint = self._device.datapoints[self._mapping.dp_id]
        if datapoint:
            return datapoint.value / self._mapping.coefficient

        return self._mapping.description.native_min_value

    def set_native_value(self, value: float) -> None:
        """Set new value."""
        if self._mapping.setter:
            self._mapping.setter(self, self._product, value)
            return
        int_value = int(value * self._mapping.coefficient)
        datapoint = self._device.datapoints.get_or_create(
            self._mapping.dp_id,
            TuyaBLEDataPointType.DT_VALUE,
            int(int_value),
        )
        if datapoint:
            self._hass.create_task(datapoint.set_value(int_value))

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        result = super().available
        if result and self._mapping.is_available:
            result = self._mapping.is_available(self, self._product)
        return result

class TuyaBLEVirtualNumber(RestoreNumber):
    """Representation of a virtual Tuya BLE number that persists its state."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLEVirtualNumberMapping,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._device = device
        self._product = product
        self._mapping = mapping
        self._attr_mode = mapping.mode
        self._attr_native_value = mapping.default_value

        # Set entity attributes from description
        self._attr_name = mapping.description.name
        self._attr_icon = mapping.description.icon
        self._attr_entity_category = mapping.description.entity_category
        self._attr_native_min_value = mapping.description.native_min_value
        self._attr_native_max_value = mapping.description.native_max_value
        self._attr_native_step = mapping.description.native_step
        self._attr_native_unit_of_measurement = mapping.description.native_unit_of_measurement

        # Generate unique ID
        self._attr_unique_id = f"{device.address}_{mapping.description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.address)},
            "name": device.name,
            "manufacturer": "Tuya",
            "model": f"{device.category} {device.product_id}",
        }

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        # Restore previous state if available
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            try:
                self._attr_native_value = float(last_state.state)
            except (ValueError, TypeError):
                self._attr_native_value = self._mapping.default_value

        # Create storage directory if it doesn't exist
        config_dir = self.hass.config.path(".storage")
        os.makedirs(config_dir, exist_ok=True)

        # Try to load from storage file
        storage_file = os.path.join(config_dir, f"tuya_ble_virtual_{self._attr_unique_id}.json")
        try:
            if os.path.exists(storage_file):
                with open(storage_file, "r") as f:
                    data = json.load(f)
                    if "value" in data:
                        self._attr_native_value = float(data["value"])
        except (ValueError, TypeError, json.JSONDecodeError, IOError):
            pass

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        self._attr_native_value = value
        self.async_write_ha_state()

        # Save to storage file
        config_dir = self.hass.config.path(".storage")
        storage_file = os.path.join(config_dir, f"tuya_ble_virtual_{self._attr_unique_id}.json")
        try:
            with open(storage_file, "w") as f:
                json.dump({"value": value}, f)
        except IOError:
            _LOGGER.warning(f"Failed to save virtual number value to {storage_file}")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tuya BLE sensors."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    mappings = get_mapping_by_device(data.device)
    entities: list[TuyaBLENumber] = []
    for mapping in mappings:
        if mapping.force_add or data.device.datapoints.has_id(
            mapping.dp_id, mapping.dp_type
        ):
            entities.append(
                TuyaBLENumber(
                    hass,
                    data.coordinator,
                    data.device,
                    data.product,
                    mapping,
                )
            )
    async_add_entities(entities)
