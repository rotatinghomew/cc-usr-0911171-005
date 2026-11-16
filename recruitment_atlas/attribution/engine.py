from __future__ import annotations
from ..journey_graph.attribution import JourneyAttribution
import datetime as dt
import decimal
import math
import statistics
import ast
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

class DomainError(RuntimeError):
    pass

@dataclass(frozen=True)
class AnalysisResult:
    key: str
    accepted: bool
    value: decimal.Decimal
    reasons: tuple[str,...]
    facts: Mapping[str,Any]=field(default_factory=dict)

class AttributionEngineBase:
    algorithm='attribution'
    def normalize(self,rows):
        result=[];seen=set()
        for position,row in enumerate(rows):
            item=dict(row);key=str(item.get('id') or position)
            if key in seen:continue
            seen.add(key);item['_key']=key;item['_position']=position;result.append(item)
        return result
    def rank(self,subjects,context):return sorted([self.evaluate(item,context) for item in self.normalize(subjects)],key=lambda result:(not result.accepted,-result.value,result.key))
    def allocate(self,subjects,context):
        remaining=decimal.Decimal(str(context.get('capacity',100)))-decimal.Decimal(str(context.get('reserved',0)));accepted=[];rejected=[]
        by_key={str(item.get('id')):item for item in self.normalize(subjects)}
        for result in self.rank(subjects,context):
            demand=decimal.Decimal(str(by_key[result.key].get('demand',1)))
            if result.accepted and demand<=remaining:accepted.append(result);remaining-=demand
            else:rejected.append(AnalysisResult(result.key,False,result.value,result.reasons+('allocation_exhausted',),result.facts))
        return accepted,rejected
    def summarize(self,results):
        values=[float(result.value) for result in results];reasons=Counter(reason for result in results for reason in result.reasons)
        return {'count':len(results),'accepted':sum(result.accepted for result in results),'rejected':sum(not result.accepted for result in results),'mean':statistics.fmean(values) if values else 0.0,'median':statistics.median(values) if values else 0.0,'reasons':dict(sorted(reasons.items()))}
    def compare(self,left,right):
        a={item.key:item for item in left};b={item.key:item for item in right};changes=[]
        for key in sorted(set(a)|set(b)):
            old=a.get(key);new=b.get(key)
            if old is None:changes.append({'key':key,'change':'added','accepted':new.accepted})
            elif new is None:changes.append({'key':key,'change':'removed','accepted':old.accepted})
            elif old.accepted!=new.accepted or old.value!=new.value:changes.append({'key':key,'change':'changed','from':str(old.value),'to':str(new.value),'accepted_from':old.accepted,'accepted_to':new.accepted})
        return {'changes':changes,'change_count':len(changes)}

class AttributionEngine(AttributionEngineBase):
    def __init__(self): self.journeys=JourneyAttribution()
    def evaluate(self,item,context):
        if item.get('events'):
            facts=self.journeys.evaluate(item,context); accepted=bool(facts['weights']); return AnalysisResult(str(item.get('id','unknown')),accepted,decimal.Decimal('1') if accepted else decimal.Decimal('0'),() if accepted else ('no_conversion',),facts)
        if item.get('events'):
            facts=self.journeys.evaluate(item,context); accepted=bool(facts['weights']); return AnalysisResult(str(item.get('id','unknown')),accepted,sum((decimal.Decimal(value) for value in facts['weights'].values()),decimal.Decimal('0')),() if accepted else ('no_conversion',),facts)
        key=str(item.get('id','unknown'));events=sorted(item.get('events',[]),key=lambda event:(event.get('at',''),event.get('event_id','')));seen=set();touches=[];conversion=None
        for event in events:
            event_id=str(event.get('event_id'))
            if event_id in seen:continue
            seen.add(event_id)
            if event.get('type') in {'visit','view','submission','interview'}:touches.append(event)
            if event.get('type')=='intent':conversion=event
        reasons=[]
        if conversion is None:reasons.append('no_conversion')
        weights={}
        if touches and conversion:
            denominator=sum(range(1,len(touches)+1))
            for index,touch in enumerate(touches,1):weights[touch['event_id']]=decimal.Decimal(index)/decimal.Decimal(denominator)
        value=sum(weights.values(),decimal.Decimal('0'));return AnalysisResult(key,conversion is not None,value,tuple(reasons),{'touches':len(touches),'weights':{k:str(v) for k,v in weights.items()},'deduplicated_events':len(seen)})
