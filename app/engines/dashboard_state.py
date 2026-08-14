from typing import Literal

DashboardState = Literal[
    "belum_ada_data",
    "sudah_menganalisis",
    "usaha_berjalan_data_kurang",
    "usaha_berjalan_data_cukup",
]


def determine_owner_dashboard_state(
    *, has_business: bool, has_analysis: bool, recorded_days: int, threshold_days: int = 7
) -> DashboardState:
    if has_business:
        if recorded_days >= threshold_days:
            return "usaha_berjalan_data_cukup"
        return "usaha_berjalan_data_kurang"
    if has_analysis:
        return "sudah_menganalisis"
    return "belum_ada_data"
