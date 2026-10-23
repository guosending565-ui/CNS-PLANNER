"""Independent P16-V2 search over immutable qualified provider signatures."""
import itertools
import numpy as np

VERSION='p16-fast-search-v2.0'
COST_POLICY_VERSION='reuse-preference-v2.0'
REUSE_COST={'existing_cns_facility':1.,'existing_shared_site':1.25,
           'tower_colocation_host':1.5,'candidate_site':2.,'new_build_candidate':3.}


class FastPlannerV2:
    algorithm_id='corridor_fast_site_planner_v2'
    algorithm_version='2.0'

    def __init__(self,matrix,action_count,baseline,costs,cap=13,pair_width=20,local_budget=16000,scarcity_power=1.0,coverage_cache=None):
        self.matrix=matrix; self.n=action_count; self.baseline=tuple(baseline)
        self.costs=np.asarray(costs); self.cap=cap
        self.pair_width=pair_width; self.local_budget=local_budget
        self.score_cache={}; self.pair_count=0; self.local_count=0
        self.pair_steps=0
        self.coverage_cache={} if coverage_cache is None else coverage_cache
        self.coverage_build_count=0
        availability=(matrix.signatures[:action_count]==1).sum(axis=0)
        # Scarce qualified opportunities matter before abundant overlapping rows.
        self.weights=matrix.weights/np.maximum(availability,1)**scarcity_power
        self.known=(matrix.signatures==1).astype(np.int16)

    def state(self,selected):
        self.coverage_build_count+=1
        state=self.matrix.empty()
        for i in (*self.baseline,*sorted(selected)): state.add(i)
        return state

    def counts(self,selected):
        key=tuple(sorted(set(selected)))
        scoped=(id(self.matrix),tuple(sorted(set(self.baseline))),key)
        if scoped not in self.coverage_cache:
            self.coverage_cache[scoped]=np.minimum(self.state(key).counts,self.matrix.required).astype(np.int16)
        return self.coverage_cache[scoped]

    def score(self,selected):
        key=tuple(sorted(set(selected)))
        if key not in self.score_cache:
            counts=self.counts(key)
            self.score_cache[key]=float(np.minimum(counts,self.matrix.required)@self.weights)
        return self.score_cache[key]

    def rank(self,selected):
        counts=self.counts(selected)
        need=self.matrix.required-counts
        additions=self.known[:self.n].copy()
        by_site={}
        for i in (*self.baseline,*sorted(selected)):
            by_site.setdefault(self.matrix.sites[i],[]).append(i)
        for site,members in by_site.items():
            covered=self.known[sorted(members)].any(axis=0)
            indices=[i for i in range(self.n) if self.matrix.sites[i]==site]
            additions[indices]*=~covered
        gains=np.minimum(additions,np.maximum(need,0))@self.weights
        return sorted((i for i in range(self.n) if i not in selected and gains[i]>1e-9),
                      key=lambda i:(-gains[i],self.costs[i],i))

    def complete(self,selected):
        return bool((self.counts(selected)>=self.matrix.required).all())

    def search(self, *, on_step=None, cancel_check=None):
        """Greedy + finite 1/2-replacement local search.

        ``on_step`` / ``cancel_check`` 是**只读观测钩子**（生产集成用）：
        前者把真实搜索计数（已选动作数 / 组合比较数）交给调用方展示进度，
        后者在每次迭代前做协作式取消检查。两者都不参与任何判定，
        默认 ``None`` 时算法行为与 R6-B 完全一致。
        """
        selected=set()
        steps=0

        def tick(phase):
            if cancel_check is not None:
                cancel_check()
            if on_step is not None:
                on_step(len(selected), self.cap, phase,
                        self.pair_count, self.local_count, steps)

        while len(selected)<self.cap and not self.complete(selected):
            steps+=1; tick('greedy')
            ranked=self.rank(selected)
            if not ranked: break
            current=self.score(selected)
            best=(ranked[0],); gain=self.score(selected|set(best))-current
            if self.cap-len(selected)>=2:
                after_first=selected|set(best)
                second=self.rank(after_first)
                greedy_pair_score=(self.score(after_first|{second[0]}) if second else self.score(after_first))
                pair_winner=None; pair_score=greedy_pair_score
                for pair in itertools.combinations(ranked[:self.pair_width],2):
                    self.pair_count+=1
                    value=self.score(selected|set(pair))
                    if value>pair_score+1e-8:
                        pair_winner=pair; pair_score=value
                if pair_winner is not None:
                    best=pair_winner; self.pair_steps+=1
            selected.update(best)
        # Finite 1->1 and 2->2 replacements; partial improvements remain eligible.
        for width in (1,2):
            if self.complete(selected): break
            improved=True; width_count=0
            while improved and width_count<self.local_budget:
                steps+=1; tick('local')
                improved=False; incumbent=self.score(selected); winner=None
                for removed in itertools.combinations(sorted(selected),width):
                    base=selected-set(removed)
                    ranked=[i for i in self.rank(base) if i not in removed][:self.pair_width]
                    for added in itertools.combinations(ranked,width):
                        key=base|set(added)
                        if len(key)>self.cap: continue
                        self.local_count+=1; width_count+=1; value=self.score(key)
                        if value>incumbent+1e-8:
                            incumbent=value; winner=key
                        if width_count>=self.local_budget: break
                    if width_count>=self.local_budget: break
                if winner is not None: selected=winner; improved=True
        # Remove redundant actions while preserving complete coverage.
        if self.complete(selected):
            for i in sorted(selected,key=lambda i:(-self.costs[i],i)):
                if self.complete(selected-{i}): selected.remove(i)
        tick('done')
        return tuple(sorted(selected))
