"""F&B business taxonomy shared by education mapping and analysis input.

The identifiers are versioned because competitor counts are only comparable
within one taxonomy version (docs/05).
"""

from __future__ import annotations

from typing import Literal, get_args

BUSINESS_TAXONOMY_VERSION = "fnb-taxonomy-v1"

BusinessType = Literal[
    "food_stall",
    "coffee_shop",
    "restaurant",
    "bakery",
    "catering",
    "food_truck",
    "cloud_kitchen",
    "beverage_stand",
]

BUSINESS_TYPES: tuple[BusinessType, ...] = get_args(BusinessType)

BUSINESS_TYPE_LABELS: dict[BusinessType, str] = {
    "food_stall": "Warung atau kedai makan",
    "coffee_shop": "Kedai kopi",
    "restaurant": "Restoran",
    "bakery": "Toko roti dan kue",
    "catering": "Katering",
    "food_truck": "Food truck",
    "cloud_kitchen": "Dapur satelit",
    "beverage_stand": "Gerai minuman",
}

SalesChannel = Literal["dine_in", "takeaway", "delivery", "catering_order"]

CHANNELS: tuple[SalesChannel, ...] = get_args(SalesChannel)
