from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

TOUCH_TYPES = frozenset({'view', 'visit', 'submission', 'interview'})
DEFAULT_CONVERSION_TYPE = 'intent'
DEFAULT_SESSION_GAP_MINUTES = 45
DEFAULT_CONVERSION_WINDOW_DAYS = 30
HALF_LIFE_DAYS = Decimal('7')
ANONYMOUS = '__anonymous__'
AMBIGUOUS = '__ambiguous__'
MAX_TIME = dt.datetime.max.replace(tzinfo=dt.timezone.utc)

PERSON_FIELDS = (
    'person_id', 'person', 'candidate_id', 'applicant_id',
    'jobseeker_id', 'user_id',
)
JOURNEY_PERSON_FIELDS = ('owner_id', 'candidate_id', 'person_id')
PERSON_KINDS = ('person', 'candidate', 'applicant', 'jobseeker', 'user')
ALIAS_FIELDS = (
    'identity_id', 'identity_key', 'device_id', 'account_id', 'member_id',
    'email', 'phone', 'cookie_id', 'ip', 'fingerprint',
)
ALIAS_FIELD_KINDS = {
    'identity_id': 'identity', 'identity_key': 'identity', 'device_id': 'device',
    'account_id': 'account', 'member_id': 'account', 'email': 'email',
    'phone': 'phone', 'cookie_id': 'cookie', 'ip': 'ip', 'fingerprint': 'fingerprint',
}
ALIAS_LIST_FIELDS = ('identity_keys', 'keys', 'aliases')
# Aliases in the same group identify the person directly; aliases in different
# groups only co-locate a person when nothing stronger says otherwise.
HANDLE_GROUPS = (
    ('email', 'phone', 'account', 'member', 'identity'),
    ('device', 'cookie', 'fingerprint'),
    ('ip',),
)
SCOPE_FIELDS = ('intent_id', 'intent_key', 'job_id', 'position_id', 'requisition_id')
INGEST_FIELDS = frozenset({'received_at', 'ingested_at', 'ingested_on', '_key', '_position'})

KNOWN_KINDS = frozenset(PERSON_KINDS) | frozenset(ALIAS_FIELD_KINDS.values()) | {'person'}
WEAK_RELATIONS = frozenset({
    'shared_device', 'same_device', 'shared_ip', 'same_ip', 'shared_cookie',
    'shared_household', 'household', 'possible', 'candidate', 'weak',
    'unknown', 'unverified', 'suspected', 'co_occurrence', 'cooccurrence',
})
STRONG_PERSON_RELATIONS = frozenset({
    'same_person', 'same_identity', 'identity_merge', 'merged', 'merge',
    'verified_same_person', 'same_user', 'same_candidate', 'alias', 'identity',
})
STRONG_RELATIONS = frozenset({
    'link', 'identity_link', 'identity', 'alias', 'identity_merge', 'merged',
    'merge', 'verified', 'account_link', 'login_link', 'device_link',
    'email_link', 'phone_link', 'owns', 'owned_by', 'match', 'exact', 'strong',
}) | frozenset(ALIAS_FIELD_KINDS.values()) | STRONG_PERSON_RELATIONS
WEAK_STATUSES = frozenset({
    'ambiguous', 'rejected', 'invalid', 'conflict', 'conflicting', 'shared',
    'unverified', 'suspected', 'pending',
})


class JourneyAttributionError(RuntimeError):
    pass


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _lower(value: Any) -> str:
    text = _text(value)
    return text.lower() if text else ''


def _time(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, dt.datetime):
            moment = value
        else:
            moment = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _number(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _endpoint(value: Any) -> tuple[str, str] | None:
    if isinstance(value, Mapping):
        kind = _lower(value.get('kind') or value.get('type') or value.get('field'))
        name = _text(value.get('value') or value.get('id') or value.get('key'))
        if name is None:
            return None
        return (kind if kind in KNOWN_KINDS else '', name)
    text = _text(value)
    if text is None:
        return None
    if ':' in text:
        kind, name = text.split(':', 1)
        kind = kind.strip().lower()
        if kind in KNOWN_KINDS and name.strip():
            return kind, name.strip()
    return '', text


def _endpoint_key(kind: str, name: str) -> str:
    return f'{kind}:{name}' if kind else name


def _person_name(kind: str, name: str) -> str | None:
    return name if kind in PERSON_KINDS else None


@dataclass(frozen=True)
class _IdentityResolution:
    mapping: Mapping[str, str]
    ambiguous: frozenset[str]
    problems: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class _PreparedEvent:
    event: Mapping[str, Any]
    key: str
    identifier: str
    person: str
    occurred_at: dt.datetime | None
    received_at: dt.datetime | None
    kind: str
    raw_type: str
    scope: tuple[tuple[str, str], ...]


class JourneyAttribution:
    """Per-candidate journey attribution.

    Every stage (identity merge, de-duplication, sessions, funnel and weighted
    attribution) is scoped to one resolved person and, where the data provides
    them, to one expressed intent.  Records that cannot be tied to a single
    person are reported as uncertain instead of being credited to somebody else.
    """

    def __init__(self) -> None:
        self.last_identity_problems: tuple[Mapping[str, Any], ...] = ()

    # ------------------------------------------------------------------ identity
    def canonical_identities(self, links: Iterable[Mapping[str, Any]] | None) -> dict[str, str]:
        resolution = self._resolve_identities(links)
        self.last_identity_problems = resolution.problems
        return dict(resolution.mapping)

    def _resolve_identities(self, links: Iterable[Mapping[str, Any]] | None,
                            ) -> _IdentityResolution:
        parent: dict[str, str] = {}
        root_persons: dict[str, set[str]] = defaultdict(set)
        problems: list[Mapping[str, Any]] = []
        ambiguous: set[str] = set()

        def find(node: str) -> str:
            parent.setdefault(node, node)
            if parent[node] != node:
                parent[node] = find(parent[node])
            return parent[node]

        def union(left: str, right: str) -> None:
            a, b = find(left), find(right)
            if a == b:
                return
            if b < a:
                a, b = b, a
            parent[b] = a
            root_persons[a] |= root_persons.pop(b, set())

        for index, link in enumerate(links or ()):
            if not isinstance(link, Mapping):
                problems.append({'code': 'invalid_identity_link', 'index': index})
                continue
            left = _endpoint(link.get('left') if 'left' in link else link.get('source', link.get('from')))
            right = _endpoint(link.get('right') if 'right' in link else link.get('target', link.get('to')))
            if left is None or right is None:
                problems.append({'code': 'missing_identity_endpoint', 'index': index})
                continue
            left_key, right_key = _endpoint_key(*left), _endpoint_key(*right)
            if left_key == right_key:
                continue
            weak = self._link_problem(link)
            if weak is not None:
                problems.append({'code': weak, 'left': left_key, 'right': right_key})
                # Only the loose handle becomes unknown; an explicitly named
                # candidate keeps their own identified records.
                if left[0] not in PERSON_KINDS:
                    ambiguous.add(left_key)
                if right[0] not in PERSON_KINDS:
                    ambiguous.add(right_key)
                continue
            left_person = _person_name(*left)
            right_person = _person_name(*right)
            relation = _lower(link.get('relation') or link.get('method') or link.get('type'))
            merge_persons = bool(left_person and right_person and relation in STRONG_PERSON_RELATIONS)
            a, b = find(left_key), find(right_key)
            if a == b:
                continue
            persons_a = set(root_persons.get(a, set()))
            persons_b = set(root_persons.get(b, set()))
            if left_person:
                persons_a.add(left_person)
            if right_person:
                persons_b.add(right_person)
            conflicting = bool(persons_a and persons_b
                               and (persons_a - persons_b) and (persons_b - persons_a))
            if conflicting and not merge_persons:
                # A shared alias is only ambiguous for that alias.  It never
                # swallows the two candidates' own identified records, and it
                # remains ambiguous whatever order the links arrived in.
                problems.append({'code': 'conflicting_identity_link',
                                 'left': left_key, 'right': right_key})
                for kind, name, key in ((*left, left_key), (*right, right_key)):
                    if kind in PERSON_KINDS:
                        continue
                    ambiguous.update({key, name})
                continue
            union(left_key, right_key)
            merged = find(left_key)
            root_persons[merged] |= persons_a | persons_b

        for node in parent:
            find(node)
        grouped: dict[str, list[str]] = defaultdict(list)
        for node in parent:
            grouped[find(node)].append(node)
        mapping: dict[str, str] = {}
        for root, nodes in grouped.items():
            persons = set(root_persons.get(root, set()))
            for node in nodes:
                person = _person_name(*_endpoint(node))
                if person:
                    persons.add(person)
            canonical = min(persons) if persons else min(nodes)
            candidates: dict[str, set[str]] = defaultdict(set)
            for node in nodes:
                kind, name = _endpoint(node)
                if name in ambiguous or node in ambiguous:
                    continue
                mapping[node] = canonical
                candidates[name].add(canonical)
            for name, values in candidates.items():
                if name in ambiguous:
                    continue
                if len(values) == 1:
                    mapping.setdefault(name, canonical)
                else:
                    ambiguous.add(name)
        return _IdentityResolution(mapping, frozenset(ambiguous), tuple(problems))

    def _link_problem(self, link: Mapping[str, Any]) -> str | None:
        if link.get('ambiguous') is True or link.get('verified') is False:
            return 'unverified_identity_link'
        if _lower(link.get('status')) in WEAK_STATUSES:
            return 'ambiguous_identity_link'
        relation = _lower(link.get('relation') or link.get('method') or link.get('type'))
        if relation in WEAK_RELATIONS:
            return 'weak_identity_link'
        if relation and relation not in STRONG_RELATIONS:
            return 'unsupported_identity_relation'
        confidence = link.get('confidence', link.get('weight'))
        if confidence is not None:
            parsed = _number(confidence)
            if parsed is None:
                return 'invalid_identity_confidence'
            if parsed < Decimal('0.5'):
                return 'low_confidence_identity_link'
        return None

    @staticmethod
    def _event_aliases(event: Mapping[str, Any]) -> list[tuple[str, str]]:
        aliases: list[tuple[str, str]] = []
        for field in ALIAS_FIELDS:
            value = _text(event.get(field))
            if value:
                aliases.append((ALIAS_FIELD_KINDS[field], value))
        for field in ALIAS_LIST_FIELDS:
            values = event.get(field)
            if isinstance(values, (list, tuple, set)):
                for value in values:
                    endpoint = _endpoint(value)
                    if endpoint is not None:
                        aliases.append(endpoint)
        return aliases

    def _resolve_person(self, event: Mapping[str, Any], resolution: _IdentityResolution,
                        problems: list[Mapping[str, Any]]) -> str:
        explicit = {_text(event.get(field)) for field in PERSON_FIELDS}
        explicit.discard(None)
        if len(explicit) > 1:
            return AMBIGUOUS
        label = _text(event.get('event_id') or event.get('id')) or ''
        base: str | None = None
        base_mapped = False
        if explicit:
            base, base_mapped = self._lookup(next(iter(explicit)), PERSON_KINDS, resolution)
            if base == AMBIGUOUS:
                return AMBIGUOUS
        aliases = self._event_aliases(event)
        for kind, value in aliases:
            if kind not in PERSON_KINDS:
                continue
            resolved, _ = self._lookup(value, PERSON_KINDS, resolution)
            if resolved == AMBIGUOUS:
                return AMBIGUOUS
            if base is None:
                base, base_mapped = resolved, True
            elif resolved != base:
                problems.append({'code': 'identity_alias_conflict', 'event_id': label,
                                 'person': base, 'alias': f'{kind}:{value}'})
        chosen, chosen_mapped = base, base_mapped
        for group in HANDLE_GROUPS:
            mapped_values: set[str] = set()
            literal_values: set[str] = set()
            for kind, value in aliases:
                if kind not in group:
                    continue
                resolved, mapped = self._lookup(value, (kind,), resolution)
                if resolved == AMBIGUOUS:
                    return AMBIGUOUS
                (mapped_values if mapped else literal_values).add(resolved)
            if mapped_values and chosen is not None and mapped_values != {chosen}:
                # Contradicting verified handles cannot be reduced to one
                # candidate, no matter which field happened to be explicit.
                problems.append({'code': 'identity_alias_conflict', 'event_id': label,
                                 'person': chosen, 'alias': sorted(mapped_values)[0]})
                return AMBIGUOUS
            if chosen is None:
                if mapped_values:
                    if len(mapped_values) > 1:
                        return AMBIGUOUS
                    chosen, chosen_mapped = next(iter(mapped_values)), True
                elif base is None and literal_values:
                    if len(literal_values) > 1:
                        # Several unverified handles in one event: keep them
                        # together as one synthetic person rather than guessing.
                        chosen = self._synthetic_person(group, literal_values)
                    else:
                        chosen = next(iter(literal_values))
                    chosen_mapped = False
            elif (base is None and not chosen_mapped and not mapped_values
                  and literal_values and literal_values != {chosen}):
                # An event carrying only unverified handles is still one
                # observation: keep its handles together as one synthetic
                # person instead of picking one or merging it into a candidate.
                chosen = self._synthetic_person(group, literal_values | {chosen})
        if chosen is None:
            # An event-level owner is the weakest identity signal: it is used
            # only when the record names nobody else, and it is never allowed
            # to override a conflicting explicit person.
            owner = _text(event.get('owner_id'))
            if owner:
                return self._lookup(owner, PERSON_KINDS, resolution)[0]
        return chosen if chosen is not None else ANONYMOUS

    @staticmethod
    def _synthetic_person(group: Sequence[str], values: Iterable[str]) -> str:
        return '__handles__:' + _digest([list(group), sorted(values)])[:16]

    @staticmethod
    def _lookup(value: str, kinds: Sequence[str],
                resolution: _IdentityResolution) -> tuple[str, bool]:
        """Return the resolved person and whether a verified link backed it."""
        if value in resolution.ambiguous:
            return AMBIGUOUS, True
        for kind in kinds:
            candidate = f'{kind}:{value}'
            if candidate in resolution.ambiguous:
                return AMBIGUOUS, True
            if candidate in resolution.mapping:
                return resolution.mapping[candidate], True
        if value in resolution.mapping:
            return resolution.mapping[value], True
        return value, False

    # ------------------------------------------------------------------ preparation
    def _prepare(self, events: Iterable[Mapping[str, Any]] | None,
                 resolution: _IdentityResolution | None = None,
                 default_person: str | None = None
                 ) -> tuple[list[_PreparedEvent], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
        if resolution is None:
            resolution = _IdentityResolution({}, frozenset(), ())
        event_problems: list[Mapping[str, Any]] = []
        resolved_events: list[tuple[Mapping[str, Any], str]] = []
        for raw in events or ():
            if not isinstance(raw, Mapping):
                continue
            event = dict(raw)
            resolved_events.append((event, self._resolve_person(event, resolution, event_problems)))
        identified = {person for _, person in resolved_events
                      if person not in {ANONYMOUS, AMBIGUOUS}}
        # A journey with a single declared candidate and no event-level identity
        # at all belongs to that candidate.  Once any event is identified, an
        # untagged event is unknown and is never silently absorbed.
        if default_person and not identified:
            resolved_events = [
                (event, default_person if person == ANONYMOUS else person)
                for event, person in resolved_events]
        groups: dict[str, list[_PreparedEvent]] = defaultdict(list)
        for event, person in resolved_events:
            occurred = _time(event.get('occurred_at', event.get('at', event.get('timestamp'))))
            received = _time(event.get('received_at', event.get('ingested_at')))
            raw_type = _text(event.get('type') or event.get('kind') or event.get('event_type')) or ''
            kind = raw_type.lower()
            scope = self._scope(event)
            identifier = _text(event.get('event_id') or event.get('id'))
            if identifier is None:
                source = _text(event.get('source'))
                source_key = _text(event.get('source_key') or event.get('source_id'))
                identifier = f'{source}:{source_key}' if source and source_key else ''
            key = self._event_key(event, identifier, person, occurred, kind, scope)
            groups[key].append(_PreparedEvent(event, key, identifier or key, person, occurred,
                                              received, kind, raw_type, scope))

        prepared: list[_PreparedEvent] = []
        ambiguous_events: list[Mapping[str, Any]] = []
        for key, candidates in groups.items():
            candidates.sort(key=self._canonical_order)
            canonical = candidates[0]
            persons = {item.person for item in candidates
                       if item.person not in {AMBIGUOUS, ANONYMOUS}}
            if len(persons) > 1:
                # A late duplicate cannot move a settled identity, but that is
                # only safe when arrival order is actually known.  Without
                # arrival times the record belongs to nobody in particular.
                event_problems.append({'key': key, 'code': 'conflicting_duplicate',
                                       'persons': sorted(persons),
                                       'kept': (None if canonical.received_at is None
                                                else canonical.person)})
                if canonical.received_at is None:
                    canonical = dataclasses.replace(canonical, person=AMBIGUOUS)
            if canonical.person == AMBIGUOUS:
                # Kept as one unknown actor: it still belongs to the observed
                # funnel but can never be credited to a named candidate.
                ambiguous_events.append({'key': key, 'code': 'ambiguous_identity'})
            prepared.append(canonical)
        prepared.sort(key=lambda item: (item.occurred_at is None,
                                        item.occurred_at or MAX_TIME, item.key))
        return prepared, event_problems, ambiguous_events

    @staticmethod
    def _canonical_order(item: _PreparedEvent) -> tuple[Any, ...]:
        # The first received copy wins so a duplicate that arrives late can
        # neither re-open a settled identity nor re-time a settled attribution.
        return (item.received_at is None, item.received_at or MAX_TIME,
                item.occurred_at is None, item.occurred_at or MAX_TIME,
                item.person == AMBIGUOUS, _digest(item.event))

    def _event_key(self, event: Mapping[str, Any], identifier: str | None,
                   person: str, occurred: dt.datetime | None, kind: str,
                   scope: tuple[tuple[str, str], ...]) -> str:
        if identifier:
            return 'id:' + identifier
        source = _text(event.get('source'))
        source_key = _text(event.get('source_key') or event.get('source_id'))
        if source and source_key:
            return f'source:{source}:{source_key}'
        body = {key: value for key, value in event.items() if key not in INGEST_FIELDS}
        return 'content:' + _digest({'person': person, 'type': kind,
                                     'occurred': occurred.isoformat() if occurred else None,
                                     'scope': scope, 'event': body})

    @staticmethod
    def _scope(event: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
        for field in SCOPE_FIELDS:
            value = _text(event.get(field))
            if value:
                return ((field, value),)
        return ()

    def _identity_for(self, events: Iterable[Mapping[str, Any]] | None,
                      links: Iterable[Mapping[str, Any]] | None) -> _IdentityResolution:
        combined = list(links or ()) + self._derived_links(events)
        return self._resolve_identities(combined)

    @staticmethod
    def _derived_links(events: Iterable[Mapping[str, Any]] | None) -> list[Mapping[str, Any]]:
        """Events may carry verified links between a person and their handles."""
        links: list[Mapping[str, Any]] = []
        for event in events or ():
            if not isinstance(event, Mapping):
                continue
            for item in event.get('identity_links') or ():
                if isinstance(item, Mapping):
                    links.append(item)
        return links

    @staticmethod
    def _owner_candidates(item: Mapping[str, Any], context: Mapping[str, Any]) -> list[str]:
        values: list[str] = []
        for source in (item, context):
            for field in JOURNEY_PERSON_FIELDS:
                value = _text(source.get(field))
                if value and value not in values:
                    values.append(value)
        return values

    # ------------------------------------------------------------------ public API
    def deduplicate(self, events: Iterable[Mapping[str, Any]] | None,
                    identities: Iterable[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
        events = list(events or ())
        prepared, _, _ = self._prepare(events, self._identity_for(events, identities))
        return [dict(item.event) for item in prepared]

    def sessionize(self, events: Iterable[Mapping[str, Any]] | None,
                   gap_minutes: int = DEFAULT_SESSION_GAP_MINUTES,
                   identities: Iterable[Mapping[str, Any]] | None = None) -> list[list[dict[str, Any]]]:
        if gap_minutes < 0:
            raise JourneyAttributionError('session gap cannot be negative')
        events = list(events or ())
        prepared, _, _ = self._prepare(events, self._identity_for(events, identities))
        return [[dict(item.event) for item in session]
                for session in self._sessions(prepared, gap_minutes)]

    def funnel(self, events: Iterable[Mapping[str, Any]] | None,
               identities: Iterable[Mapping[str, Any]] | None = None) -> dict[str, int]:
        events = list(events or ())
        prepared, _, _ = self._prepare(events, self._identity_for(events, identities))
        return self._funnel(prepared)

    def weights(self, events: Iterable[Mapping[str, Any]] | None,
                conversion_type: str = DEFAULT_CONVERSION_TYPE,
                conversion_window_days: int = DEFAULT_CONVERSION_WINDOW_DAYS,
                identities: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Decimal]:
        events = list(events or ())
        prepared, _, ambiguous_events = self._prepare(
            events, self._identity_for(events, identities))
        if ambiguous_events:
            return {}
        conversion_type = (conversion_type or DEFAULT_CONVERSION_TYPE).lower()
        persons = {item.person for item in prepared
                   if item.kind == conversion_type and item.person not in {AMBIGUOUS, ANONYMOUS}}
        if len(persons) > 1:
            return {}
        target = next(iter(persons)) if persons else ANONYMOUS
        weights, _, blockers, _, _ = self._attribute(prepared, target, conversion_type,
                                                      max(0, int(conversion_window_days)))
        return {} if blockers else weights

    def evaluate(self, item: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        context = dict(context or {})
        events = [event for event in item.get('events') or () if isinstance(event, Mapping)]
        links = list(item.get('identity_links') or item.get('identities')
                     or context.get('identity_links') or ())
        resolution = self._resolve_identities(links + self._derived_links(events))
        self.last_identity_problems = resolution.problems
        owner_candidates: set[str] = set()
        for value in self._owner_candidates(item, context):
            resolved, _ = self._lookup(value, PERSON_KINDS, resolution)
            if resolved != AMBIGUOUS:
                owner_candidates.add(resolved)
        owner_person = next(iter(owner_candidates)) if len(owner_candidates) == 1 else None
        prepared, event_problems, ambiguous_events = self._prepare(
            events, resolution, default_person=owner_person)
        conversion_type = (_lower(context.get('conversion_type') or item.get('conversion_type'))
                           or DEFAULT_CONVERSION_TYPE)
        window = context.get('conversion_window_days',
                             item.get('conversion_window_days',
                                      context.get('window_days', DEFAULT_CONVERSION_WINDOW_DAYS)))
        try:
            window = max(0, int(window))
        except (TypeError, ValueError):
            window = DEFAULT_CONVERSION_WINDOW_DAYS
        gap = context.get('session_gap', item.get('session_gap', DEFAULT_SESSION_GAP_MINUTES))
        try:
            gap = max(0, int(gap))
        except (TypeError, ValueError):
            gap = DEFAULT_SESSION_GAP_MINUTES

        event_persons = {event.person for event in prepared
                         if event.person not in {AMBIGUOUS, ANONYMOUS}}
        conversion_persons = {event.person for event in prepared
                              if event.kind == conversion_type
                              and event.person not in {AMBIGUOUS, ANONYMOUS}}
        blocker: str | None = None
        target: str | None
        if len(owner_candidates) > 1:
            target, blocker = None, 'ambiguous_owner'
        elif owner_person and event_persons and owner_person not in event_persons:
            target, blocker = None, 'owner_person_mismatch'
        elif owner_person and owner_person in event_persons:
            target = owner_person
        elif len(conversion_persons) == 1:
            target = next(iter(conversion_persons))
        elif len(event_persons) == 1:
            target = next(iter(event_persons))
        elif not event_persons:
            target = owner_person or ANONYMOUS
        else:
            target = None
        blockers: list[str] = []
        warnings: list[str] = []
        weights: dict[str, Decimal] = {}
        conversion: str | None = None
        ignored: dict[str, int] = {}
        if blocker is not None:
            blockers.append(blocker)
        elif target is None:
            blockers.append('ambiguous_person')
        if not blockers:
            weights, conversion, attribute_blockers, warnings, ignored = self._attribute(
                prepared, target, conversion_type, window)
            blockers.extend(attribute_blockers)
        if ambiguous_events:
            # Unknown actors are never credited to the target; they only make
            # the result uncertain when nothing else could be attributed.
            if weights:
                warnings.append('ambiguous_identity_ignored')
            else:
                blockers.append('ambiguous_identity')

        sessions = self._sessions(prepared, gap)
        facts: dict[str, Any] = {
            'deduplicated': len(prepared),
            'deduplicated_events': len(prepared),
            'sessions': len(sessions),
            'weights': {key: str(value) for key, value in weights.items()},
            'funnel': self._funnel(prepared),
            'persons': sorted(event_persons),
            'target_person': None if target in {None, ANONYMOUS} else target,
            'conversion': conversion,
            'conversion_window_days': window,
            'reasons': sorted(set(blockers)),
            'warnings': sorted(set(warnings)),
            'uncertain': bool(blockers) or not weights,
            'ambiguous_events': len(ambiguous_events),
            'ignored': dict(sorted(ignored.items())),
            'identity_problems': [dict(problem) for problem in resolution.problems],
            'event_problems': [dict(problem) for problem in event_problems],
        }
        return facts

    # ------------------------------------------------------------------ internals
    def _sessions(self, prepared: Sequence[_PreparedEvent],
                  gap_minutes: int) -> list[list[_PreparedEvent]]:
        groups: dict[tuple[str, tuple[tuple[str, str], ...]], list[_PreparedEvent]] = defaultdict(list)
        for event in prepared:
            # An unknown actor has no reliable boundaries, so it cannot join a
            # session; grouping them would splice unrelated people together.
            if event.occurred_at is not None and event.person != AMBIGUOUS:
                groups[(event.person, event.scope)].append(event)
        sessions: list[list[_PreparedEvent]] = []
        for events in groups.values():
            events.sort(key=lambda item: (item.occurred_at, item.key))
            current: list[_PreparedEvent] = []
            previous: dt.datetime | None = None
            for event in events:
                if previous is not None and event.occurred_at - previous > dt.timedelta(minutes=gap_minutes):
                    sessions.append(current)
                    current = []
                current.append(event)
                previous = event.occurred_at
            if current:
                sessions.append(current)
        sessions.sort(key=lambda session: (session[0].occurred_at or MAX_TIME, session[0].key))
        return sessions

    @staticmethod
    def _funnel(prepared: Sequence[_PreparedEvent]) -> dict[str, int]:
        counts: dict[str, set[str]] = {}
        order: list[str] = []
        for event in prepared:
            stage = event.raw_type
            if not stage:
                continue
            if stage not in counts:
                counts[stage] = set()
                order.append(stage)
            counts[stage].add(event.person)
        return {stage: len(counts[stage]) for stage in order}

    def _attribute(self, prepared: Sequence[_PreparedEvent], target: str,
                   conversion_type: str, window_days: int
                   ) -> tuple[dict[str, Decimal], str | None, list[str], list[str], dict[str, int]]:
        warnings: list[str] = []
        ignored = {'after_conversion': 0, 'outside_window': 0,
                   'other_intent': 0, 'other_person': 0, 'unattributed': 0}
        relevant = [event for event in prepared if event.person == target]
        intents = [event for event in relevant if event.kind == conversion_type]
        ignored['unattributed'] = sum(
            1 for event in prepared
            if event.kind in TOUCH_TYPES and event.person in {AMBIGUOUS, ANONYMOUS})
        if ignored['unattributed']:
            warnings.append('unattributed_touchpoints')
        if not intents:
            return {}, None, ['no_conversion'], warnings, ignored
        if any(event.occurred_at is None for event in intents):
            return {}, None, ['missing_timestamp'], warnings, ignored
        timed = sorted(intents, key=lambda event: (event.occurred_at, event.key))
        latest_at = timed[-1].occurred_at
        latest = [event for event in timed if event.occurred_at == latest_at]
        if len({event.scope for event in latest}) > 1:
            return {}, None, ['ambiguous_conversion'], warnings, ignored
        conversion = max(latest, key=lambda event: event.key)
        scope = conversion.scope
        all_scopes = {event.scope for event in timed}
        touches = [event for event in relevant if event.kind in TOUCH_TYPES]
        ignored['other_person'] = sum(
            1 for event in prepared
            if event.kind in TOUCH_TYPES
            and event.person not in {target, AMBIGUOUS, ANONYMOUS})

        maybe_untimed = [event for event in touches if event.occurred_at is None
                         and (event.scope == scope or (not event.scope and len(all_scopes) <= 1))]
        if maybe_untimed:
            return {}, conversion.identifier, ['missing_timestamp'], warnings, ignored

        eligible: list[tuple[_PreparedEvent, Decimal]] = []
        ambiguous_scope = False
        for event in touches:
            if event.occurred_at is None:
                warnings.append('untimed_other_scope_touch_ignored')
                continue
            if event.scope and event.scope != scope:
                ignored['other_intent'] += 1
                continue
            if not event.scope and scope and len(all_scopes) > 1:
                ambiguous_scope = True
                continue
            if event.occurred_at > conversion.occurred_at:
                ignored['after_conversion'] += 1
                continue
            age_days = Decimal(str((conversion.occurred_at - event.occurred_at).total_seconds() / 86400))
            if age_days > Decimal(window_days):
                ignored['outside_window'] += 1
                continue
            eligible.append((event, age_days))
        if ambiguous_scope:
            return {}, conversion.identifier, ['intent_scope_ambiguous'], warnings, ignored
        if ignored['after_conversion']:
            warnings.append('touch_after_conversion_ignored')
        if ignored['outside_window']:
            warnings.append('touch_outside_conversion_window')
        if ignored['other_intent']:
            warnings.append('other_intent_touch_ignored')
        if not eligible:
            return {}, conversion.identifier, ['no_eligible_touchpoints'], warnings, ignored
        raw = {event.identifier: Decimal('0.5') ** (age / HALF_LIFE_DAYS)
               for event, age in eligible}
        total = sum(raw.values(), Decimal('0'))
        if total <= 0:
            return {}, conversion.identifier, ['no_eligible_touchpoints'], warnings, ignored
        weights = {key: value / total for key, value in raw.items()}
        residual = Decimal('1') - sum(weights.values(), Decimal('0'))
        if residual:
            last = next(reversed(weights))
            weights[last] += residual
        return weights, conversion.identifier, [], warnings, ignored
