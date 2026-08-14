from app.engines.dashboard_state import determine_owner_dashboard_state


def test_all_owner_dashboard_states_are_explicit() -> None:
    assert (
        determine_owner_dashboard_state(has_business=False, has_analysis=False, recorded_days=0)
        == "belum_ada_data"
    )
    assert (
        determine_owner_dashboard_state(has_business=False, has_analysis=True, recorded_days=0)
        == "sudah_menganalisis"
    )
    assert (
        determine_owner_dashboard_state(has_business=True, has_analysis=False, recorded_days=6)
        == "usaha_berjalan_data_kurang"
    )
    assert (
        determine_owner_dashboard_state(has_business=True, has_analysis=False, recorded_days=7)
        == "usaha_berjalan_data_cukup"
    )
