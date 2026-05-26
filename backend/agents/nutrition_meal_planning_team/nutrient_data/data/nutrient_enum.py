"""Closed nutrient enum — the canonical set of tracked nutrients.

Every nutrient stored in ``nutrition_nutrient_rows`` must be a member
of this enum. The string value doubles as the DB column identifier and
is stable across versions (renaming a member is a MAJOR version bump).

Grouping follows USDA FDC SR Legacy structure:
- Macros (energy + macronutrient mass)
- Minerals
- Vitamins (fat-soluble + water-soluble)
- Other (fiber, cholesterol, etc.)
"""

from __future__ import annotations

from enum import Enum


class Nutrient(str, Enum):
    """Canonical nutrient identifier."""

    __str__ = str.__str__

    # --- Macros ---
    kcal = "kcal"
    protein_g = "protein_g"
    fat_g = "fat_g"
    carbohydrate_g = "carbohydrate_g"
    fiber_g = "fiber_g"
    sugar_g = "sugar_g"
    saturated_fat_g = "saturated_fat_g"
    monounsaturated_fat_g = "monounsaturated_fat_g"
    polyunsaturated_fat_g = "polyunsaturated_fat_g"
    trans_fat_g = "trans_fat_g"
    cholesterol_mg = "cholesterol_mg"

    # --- Minerals ---
    calcium_mg = "calcium_mg"
    iron_mg = "iron_mg"
    magnesium_mg = "magnesium_mg"
    phosphorus_mg = "phosphorus_mg"
    potassium_mg = "potassium_mg"
    sodium_mg = "sodium_mg"
    zinc_mg = "zinc_mg"
    copper_mg = "copper_mg"
    manganese_mg = "manganese_mg"
    selenium_mcg = "selenium_mcg"

    # --- Vitamins (fat-soluble) ---
    vitamin_a_mcg = "vitamin_a_mcg"
    vitamin_d_mcg = "vitamin_d_mcg"
    vitamin_e_mg = "vitamin_e_mg"
    vitamin_k_mcg = "vitamin_k_mcg"

    # --- Vitamins (water-soluble) ---
    vitamin_c_mg = "vitamin_c_mg"
    thiamin_mg = "thiamin_mg"
    riboflavin_mg = "riboflavin_mg"
    niacin_mg = "niacin_mg"
    vitamin_b6_mg = "vitamin_b6_mg"
    folate_mcg = "folate_mcg"
    vitamin_b12_mcg = "vitamin_b12_mcg"

    # --- Other ---
    water_g = "water_g"
    alcohol_g = "alcohol_g"
    caffeine_mg = "caffeine_mg"
