"""Read-only registry consumer for conditional plans on frozen C/RID inputs.

This interface intentionally has no session, save, apply or state writer.
"""
from ..domain.navigation_integrity_monitoring import build_navigation_integrity_endpoint_evidence

def evaluate_navigation_integrity_research(*, required_cns, route, facilities,
        selected_sites, site_pool, selection=None, communication_delivery=None, middle_bindings=None):
    endpoint=build_navigation_integrity_endpoint_evidence(required_cns,[route],
        existing_facilities=facilities,communication_delivery=communication_delivery)
    if selection is None:
        return endpoint
    from ..algorithms.registry import build_default_algorithm_registry
    from ..domain.navigation_middle_reuse import POLICY_ID
    if (selection.get('algorithm_type'),selection.get('algorithm_id'),selection.get('version')) != ('site_planner',POLICY_ID,'1.0'):
        raise ValueError('研究入口要求显式版本化中段复用策略')
    planner=build_default_algorithm_registry(None).create('site_planner',POLICY_ID,'1.0',selection.get('parameters'))
    return planner.plan(route=route,selected_sites=selected_sites,site_pool=site_pool,endpoint_evidence=endpoint,middle_bindings=middle_bindings)
