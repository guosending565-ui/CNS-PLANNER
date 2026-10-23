"""P16-V2 immutable provider signatures and physical-site incremental coverage.

Pure NumPy domain layer; no IO, GIS, or ProjectState writes. Unknown is -1.
"""
from hashlib import sha256
import json
import numpy as np

VERSION = 'p16-fast-coverage-v2.0'


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                            separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def max_continuous_deficit(gaps, offsets, half_diagonals, route_length):
    """P15 interval-union semantics; unknown remains separate from confirmed gaps."""
    intervals=sorted((max(0.,float(o)-float(h)), min(route_length,float(o)+float(h)))
                     for gap,o,h in zip(gaps,offsets,half_diagonals) if gap)
    longest=0.; end=None; start=None
    for left,right in intervals:
        if end is None or left>end:
            start,end=left,right
        else: end=max(end,right)
        longest=max(longest,end-start)
    return longest


class CoverageMatrix:
    def __init__(self, signatures, sites, required, weights):
        values=np.asarray(signatures)
        if values.ndim != 2 or not np.isin(values,[-1,0,1]).all():
            raise ValueError('coverage signatures must be three-state rows')
        if len(sites)!=len(values) or values.shape[1]!=len(required):
            raise ValueError('coverage dimensions do not match')
        self.signatures=values.astype(np.int8,copy=True)
        self.signatures.flags.writeable=False
        self.sites=tuple(sites)
        required_values=np.asarray(required,dtype=float)
        self.weights=np.array(weights,dtype=float,copy=True)
        if (required_values.ndim!=1 or self.weights.ndim!=1 or len(weights)!=len(required)
            or not np.isfinite(required_values).all() or not np.isfinite(self.weights).all()
            or (required_values<1).any() or (required_values>32767).any()
            or (required_values!=np.floor(required_values)).any() or (self.weights<0).any()):
            raise ValueError('positive known requirements and weights required')
        self.required=required_values.astype(np.int16)
        self.required.flags.writeable=False
        self.weights.flags.writeable=False
        self.cache={}; self.evaluation_count=0

    def empty(self):
        return CoverageState(self)

    def evaluate(self, selected):
        key=tuple(sorted(set(selected)))
        if key not in self.cache:
            state=self.empty()
            for i in key: state.add(i)
            counts=state.counts.copy()
            unknown=(counts==0)&(state.unknown_counts>0)
            satisfied=counts>=self.required
            for value in (counts,unknown,satisfied): value.flags.writeable=False
            self.cache[key]={'counts':counts,'unknown':unknown,'satisfied':satisfied}
            self.evaluation_count+=1
        return self.cache[key]

    def union(self, left, right):
        return self.evaluate(set(left)|set(right))['counts']


class CoverageState:
    def __init__(self,matrix):
        self.matrix=matrix; self.selected=set(); self.by_site={}
        self.counts=np.zeros(len(matrix.required),dtype=np.int16)
        self.unknown_counts=np.zeros_like(self.counts)

    def _change(self,index,adding):
        if (index in self.selected)==adding:
            raise ValueError('duplicate add or absent remove')
        site=self.matrix.sites[index]
        members=self.by_site.setdefault(site,set())
        def contribution():
            if not members:
                z=np.zeros_like(self.counts,dtype=bool); return z,z
            rows=self.matrix.signatures[sorted(members)]
            known=(rows==1).any(axis=0)
            return known, (~known)&(rows==-1).any(axis=0)
        known,unknown=contribution()
        self.counts-=known; self.unknown_counts-=unknown
        if adding: members.add(index); self.selected.add(index)
        else: members.remove(index); self.selected.remove(index)
        known,unknown=contribution()
        self.counts+=known; self.unknown_counts+=unknown

    def add(self,index): self._change(index,True)
    def remove(self,index): self._change(index,False)
