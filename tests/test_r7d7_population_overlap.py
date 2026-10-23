import pytest
from cns_planner.data.mapping.population import PopulationGridService
from cns_planner.domain.population_nodata import normalize_population_nodata_policy

def test_area_allocation_without_centre_is_never_replaced_by_zero():
    policy=normalize_population_nodata_policy({"mode":"nodata_is_zero_population", "source":"regression", "confirmed":True, "evidence":{"audit":"R7-D7"}})
    for count in (0.0, 0.25):
        allocation={'coverage_status':'partial','population_count_people':count,'value_status':'passed','valid_covered_area_m2':10}
        assert PopulationGridService._confirmed_zero_trigger(policy,{},[],allocation,None)[0] is None
