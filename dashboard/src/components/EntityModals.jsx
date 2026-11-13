import { useState } from 'react';
import { X } from 'lucide-react';
import { api } from '../lib/api';
import { ENTITY_TYPE_COLORS } from './EntityTypeBadge';
import Modal from './Modal';
import Button from './Button';
import TextField, { TextArea } from './TextField';

const ENTITY_TYPES = Object.keys(ENTITY_TYPE_COLORS);
export const IMPORTANCE_LEVELS = ['critical', 'high', 'normal', 'low'];
const RELATIONSHIP_SUGGESTIONS = ['works_on', 'knows', 'owns', 'lives_at', 'part_of', 'created_by', 'manages', 'relates_to'];

export function AddEntityModal({ defaultAgent, onClose, onCreated }) {
  const [name, setName] = useState('');
  const [entityType, setEntityType] = useState(ENTITY_TYPES[0]);
  const [description, setDescription] = useState('');
  const [importance, setImportance] = useState('normal');
  const [saving, setSaving] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!name.trim()) return;
    setSaving(true);
    try {
      const res = await api.post('/entity/create', {
        name, entity_type: entityType, description, importance_level: importance,
      }, undefined, defaultAgent);
      onCreated(res.entity);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal title="Add Entity" onClose={onClose}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <TextField label="Name" value={name} onChange={(e) => setName(e.target.value)} required autoFocus />

        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-muted">Type</span>
          <select value={entityType} onChange={(e) => setEntityType(e.target.value)}
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-text outline-none focus-visible:border-accent">
            {ENTITY_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>

        <TextArea label="Description" value={description} onChange={(e) => setDescription(e.target.value)} rows={3} />

        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-muted">Importance level</span>
          <select value={importance} onChange={(e) => setImportance(e.target.value)}
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-text outline-none focus-visible:border-accent">
            {IMPORTANCE_LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </label>

        <div className="text-xs text-muted">Agent: <span className="capitalize text-text">{defaultAgent}</span></div>

        <div className="mt-1 flex justify-end gap-2">
          <Button variant="secondary" type="button" onClick={onClose}>Cancel</Button>
          <Button variant="primary" type="submit" disabled={!name.trim() || saving}>{saving ? 'Saving…' : 'Save'}</Button>
        </div>
      </form>
    </Modal>
  );
}

export function AddConnectionModal({ fromEntity, agentIds, onClose, onCreated }) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [target, setTarget] = useState(null);
  const [relationshipType, setRelationshipType] = useState('');
  const [strength, setStrength] = useState(0.5);
  const [direction, setDirection] = useState('directed');
  const [notes, setNotes] = useState('');
  const [sinceWhen, setSinceWhen] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!query.trim()) { setResults([]); return; }
    const t = setTimeout(async () => {
      const perAgent = await Promise.all(agentIds.map((id) => api.post('/entity/search', { query }, undefined, id).then((r) => r.results)));
      setResults(perAgent.flat().filter((e) => e.id !== fromEntity.id));
    }, 250);
    return () => clearTimeout(t);
  }, [query]);

  const submit = async (e) => {
    e.preventDefault();
    if (!target || !relationshipType.trim()) return;
    setSaving(true);
    try {
      const res = await api.post('/entity/link', {
        from_entity_id: fromEntity.id,
        to_entity_id: target.id,
        relationship_type: relationshipType,
        strength: Number(strength),
        direction,
        notes,
        since_when: sinceWhen,
      }, undefined, fromEntity.agent_id);
      onCreated(res.edge, target.name);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal title="Add connection" onClose={onClose}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <div>
          <span className="mb-1.5 block text-xs font-medium text-muted">From</span>
          <div className="rounded-lg border border-border bg-background px-3 py-2 text-sm text-muted">{fromEntity.name}</div>
        </div>

        <div>
          <span className="mb-1.5 block text-xs font-medium text-muted">To</span>
          {target ? (
            <div className="flex items-center justify-between rounded-lg border border-border bg-background px-3 py-2 text-sm">
              {target.name}
              <button type="button" onClick={() => setTarget(null)} className="text-muted hover:text-text"><X size={14} /></button>
            </div>
          ) : (
            <div className="relative">
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search entity by name…"
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-text outline-none focus-visible:border-accent" />
              {results.length > 0 && (
                <div className="absolute left-0 right-0 z-10 mt-1 max-h-40 overflow-y-auto rounded-lg border border-border bg-surface">
                  {results.map((r) => (
                    <button key={r.id} type="button" onClick={() => { setTarget(r); setQuery(''); setResults([]); }}
                      className="flex w-full items-center justify-between px-3 py-2 text-left text-sm text-text hover:bg-white/[0.06]">
                      {r.name} <span className="text-xs text-muted">{r.entity_type}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-muted">Relationship type</span>
          <input value={relationshipType} onChange={(e) => setRelationshipType(e.target.value)} list="rel-suggestions" required
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-text outline-none focus-visible:border-accent" />
          <datalist id="rel-suggestions">
            {RELATIONSHIP_SUGGESTIONS.map((s) => <option key={s} value={s} />)}
          </datalist>
        </label>

        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-muted">Strength: {Number(strength).toFixed(2)}</span>
          <input type="range" min="0" max="1" step="0.05" value={strength} onChange={(e) => setStrength(e.target.value)} className="w-full accent-accent" />
        </label>

        <div>
          <span className="mb-1.5 block text-xs font-medium text-muted">Direction</span>
          <div className="flex gap-4 text-sm text-text">
            <label className="flex items-center gap-1.5"><input type="radio" name="direction" checked={direction === 'directed'} onChange={() => setDirection('directed')} className="accent-accent" /> Directed</label>
            <label className="flex items-center gap-1.5"><input type="radio" name="direction" checked={direction === 'bidirectional'} onChange={() => setDirection('bidirectional')} className="accent-accent" /> Bidirectional</label>
          </div>
        </div>

        <TextField label="Notes" value={notes} onChange={(e) => setNotes(e.target.value)} />
        <TextField label="Since when" type="date" value={sinceWhen} onChange={(e) => setSinceWhen(e.target.value)} />

        <div className="mt-1 flex justify-end gap-2">
          <Button variant="secondary" type="button" onClick={onClose}>Cancel</Button>
          <Button variant="primary" type="submit" disabled={!target || !relationshipType.trim() || saving}>{saving ? 'Saving…' : 'Save'}</Button>
        </div>
      </form>
    </Modal>
  );
}

export function AddEventModal({ entity, onClose, onCreated }) {
  const [eventType, setEventType] = useState('');
  const [description, setDescription] = useState('');
  const [emotionalWeight, setEmotionalWeight] = useState(5);
  const [occurredAt, setOccurredAt] = useState('');
  const [saving, setSaving] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!eventType.trim() || !description.trim()) return;
    setSaving(true);
    try {
      const res = await api.post('/entity/event', {
        entity_id: entity.id, event_type: eventType, description, emotional_weight: Number(emotionalWeight), occurred_at: occurredAt,
      }, undefined, entity.agent_id);
      onCreated(res.event);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal title="Add event" onClose={onClose}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <TextField label="Event type" value={eventType} onChange={(e) => setEventType(e.target.value)} required autoFocus />
        <TextArea label="Description" value={description} onChange={(e) => setDescription(e.target.value)} rows={3} required />
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-muted">Emotional weight: {emotionalWeight}</span>
          <input type="range" min="1" max="10" value={emotionalWeight} onChange={(e) => setEmotionalWeight(e.target.value)} className="w-full accent-accent" />
        </label>
        <TextField label="Occurred at" type="date" value={occurredAt} onChange={(e) => setOccurredAt(e.target.value)} />
        <div className="mt-1 flex justify-end gap-2">
          <Button variant="secondary" type="button" onClick={onClose}>Cancel</Button>
          <Button variant="primary" type="submit" disabled={!eventType.trim() || !description.trim() || saving}>{saving ? 'Saving…' : 'Save'}</Button>
        </div>
      </form>
    </Modal>
  );
}
