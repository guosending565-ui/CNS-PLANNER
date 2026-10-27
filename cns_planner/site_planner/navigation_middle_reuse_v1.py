"""Explicit selectable strategy; the existing endpoint planner stays unchanged."""
from ..domain.navigation_middle_reuse import POLICY_ID, plan_middle_reuse
from copy import deepcopy

class NavigationIntegrityEndpointMiddleReuseV1:
    algorithm_id=POLICY_ID
    algorithm_version='1.0'

    def __init__(self,parameters=None):
        parameters=parameters or {}
        if set(parameters)-{'middle_segment_indices'}:
            raise ValueError('未知策略参数；变更合同需要新版本')
        indices=parameters.get('middle_segment_indices')
        if indices is not None and (not isinstance(indices,list) or len(indices)!=2 or
            any(not isinstance(i,int) or isinstance(i,bool) or i<1 for i in indices) or len(set(indices))!=2):
            raise ValueError('须指定两个不同的内部航段零基索引')
        self.indices=deepcopy(indices)

    def plan(self, *, route, selected_sites, site_pool, endpoint_evidence, middle_bindings=None):
        return plan_middle_reuse(route,selected_sites,site_pool,endpoint_evidence,self.indices,middle_bindings)
