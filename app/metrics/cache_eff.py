from collections import defaultdict
from typing import TypedDict

from app.pricing import cost_for
from app.sources.claude_jsonl import Record


class ByProjectModelDict(TypedDict):
    project: str
    model: str
    cache_read_ratio: float
    message_count: int

class WorstSessionDict(TypedDict):
    session_id: str
    project: str
    model: str
    cache_read_ratio: float
    input_tokens: int
    estimated_savings_usd: float

class CacheMetricsDict(TypedDict, total=False):
    savings_now_usd: float
    savings_potential_usd: float
    by_project_model: list[ByProjectModelDict]
    worst_sessions: list[WorstSessionDict]
    warnings: list[str]

def _calc_ratio(cache_read: int, input_tokens: int) -> float:
    total = input_tokens + cache_read
    return cache_read / total if total > 0 else 0.0

def cache_metrics(records: list[Record]) -> CacheMetricsDict:
    """Calculate cache efficiency metrics.
    
    Algorithms:
    - savings_now_usd: For each record, cost without cache minus cost with cache.
      Chosen option (b): call `cost_for` twice (once with cache_read=0, once with
      actual cache_read) and take the difference.
    - savings_potential_usd: For each (project, model) group, find the average
      cache_read_ratio. For sessions below this average, calculate the cost difference
      if they had achieved the average ratio.
      Chosen option (c) based on instructions.
    - by_project_model: Sorted by cache_read_ratio ascending.
    - worst_sessions: Top 10 sessions with lowest cache_read_ratio.
    """
    savings_now = 0.0
    warnings = []
    
    # group by (project, model)
    # track totals to compute group averages
    group_totals = defaultdict(lambda: {"input": 0, "cache_read": 0, "msg_count": 0})
    
    # track session totals
    session_totals = defaultdict(
        lambda: {"project": "", "model": "", "input": 0, "cache_read": 0, "out": 0, "cw": 0}
    )
    
    for r in records:
        key = (r.project, r.model)
        group_totals[key]["input"] += r.input_tokens
        group_totals[key]["cache_read"] += r.cache_read_tokens
        group_totals[key]["msg_count"] += 1
        
        s_tot = session_totals[r.session_id]
        s_tot["project"] = r.project
        s_tot["model"] = r.model
        s_tot["input"] += r.input_tokens
        s_tot["cache_read"] += r.cache_read_tokens
        s_tot["out"] += r.output_tokens
        s_tot["cw"] += r.cache_write_tokens
        
        c_no_cache, w1 = cost_for(
            r.model, r.input_tokens + r.cache_read_tokens, r.output_tokens, 0, r.cache_write_tokens
        )
        c_cache, w2 = cost_for(
            r.model, r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens
        )
        
        if w1:
            warnings.extend(w1)
        if w2:
            warnings.extend(w2)
        
        savings_now += max(0.0, c_no_cache - c_cache)
        
    group_ratios = {}
    by_project_model = []
    for (proj, mod), totals in group_totals.items():
        ratio = _calc_ratio(totals["cache_read"], totals["input"])
        group_ratios[(proj, mod)] = ratio
        by_project_model.append(ByProjectModelDict(
            project=proj,
            model=mod,
            cache_read_ratio=ratio,
            message_count=totals["msg_count"]
        ))
        
    by_project_model.sort(key=lambda x: x["cache_read_ratio"])
    
    savings_potential = 0.0
    sessions_list = []
    
    for s_id, s_data in session_totals.items():
        proj = s_data["project"]
        mod = s_data["model"]
        s_input = s_data["input"]
        s_cache = s_data["cache_read"]
        s_ratio = _calc_ratio(s_cache, s_input)
        
        group_avg = group_ratios[(proj, mod)]
        
        est_savings = 0.0
        if s_ratio < group_avg:
            total_in = s_input + s_cache
            target_cache = int(total_in * group_avg)
            target_input = total_in - target_cache
            
            c_current, _ = cost_for(mod, s_input, s_data["out"], s_cache, s_data["cw"])
            c_target, _ = cost_for(mod, target_input, s_data["out"], target_cache, s_data["cw"])
            
            diff = max(0.0, c_current - c_target)
            est_savings = diff
            savings_potential += diff
            
        sessions_list.append(WorstSessionDict(
            session_id=s_id,
            project=proj,
            model=mod,
            cache_read_ratio=s_ratio,
            input_tokens=s_input,
            estimated_savings_usd=est_savings
        ))
        
    sessions_list.sort(key=lambda x: x["cache_read_ratio"])
    worst_sessions = sessions_list[:10]
    
    res = CacheMetricsDict(
        savings_now_usd=savings_now,
        savings_potential_usd=savings_potential,
        by_project_model=by_project_model,
        worst_sessions=worst_sessions,
    )
    if warnings:
        res["warnings"] = list(set(warnings))
        
    return res
