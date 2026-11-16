from __future__ import annotations
import datetime as dt
from collections import defaultdict
from decimal import Decimal
class JourneyAttribution:
    def canonical_identities(self,links):
        parent={}
        def find(x):
            parent.setdefault(x,x)
            if parent[x]!=x:parent[x]=find(parent[x])
            return parent[x]
        def union(a,b):
            ra,rb=find(a),find(b)
            if ra!=rb:parent[max(ra,rb)]=min(ra,rb)
        for link in links:union(str(link['left']),str(link['right']))
        return {key:find(key) for key in list(parent)}
    def deduplicate(self,events):
        chosen={}
        for row in events:
            key=str(row.get('event_id') or (row.get('source'),row.get('source_key')));current=chosen.get(key)
            if current is None or str(row.get('received_at',''))<str(current.get('received_at','')):chosen[key]=dict(row)
        return sorted(chosen.values(),key=lambda event:(str(event.get('occurred_at','')),str(event.get('event_id',''))))
    def sessionize(self,events,gap_minutes=45):
        sessions=[];current=[];previous=None
        for event in self.deduplicate(events):
            at=dt.datetime.fromisoformat(str(event.get('occurred_at') or event.get('at')))
            if previous is not None and at-previous>dt.timedelta(minutes=gap_minutes):sessions.append(current);current=[]
            current.append(event);previous=at
        if current:sessions.append(current)
        return sessions
    def weights(self,events,conversion_type='intent'):
        ordered=self.deduplicate(events);conversion=next((event for event in reversed(ordered) if event.get('type')==conversion_type),None)
        if conversion is None:return {}
        conversion_at=dt.datetime.fromisoformat(str(conversion.get('occurred_at') or conversion.get('at')));raw={}
        for event in ordered:
            if event is conversion or event.get('type') not in {'view','visit','submission','interview'}:continue
            age=max(Decimal('0'),Decimal(str((conversion_at-dt.datetime.fromisoformat(str(event.get('occurred_at') or event.get('at')))).total_seconds()/86400)));raw[str(event['event_id'])]=Decimal('0.5')**(age/Decimal('7'))
        total=sum(raw.values(),Decimal('0'));return {key:(value/total if total else Decimal('0')) for key,value in raw.items()}
    def funnel(self,events):
        counts=defaultdict(set)
        for event in self.deduplicate(events):counts[str(event.get('type'))].add(str(event.get('person_id')))
        return {stage:len(people) for stage,people in counts.items()}
    def evaluate(self,item,context):
        events=item.get('events',());return {'deduplicated':len(self.deduplicate(events)),'deduplicated_events':len(self.deduplicate(events)),'sessions':len(self.sessionize(events,int(context.get('session_gap',45)))),'weights':{key:str(value) for key,value in self.weights(events).items()},'funnel':self.funnel(events)}
