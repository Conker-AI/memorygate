import { useEffect, useState } from 'react';
import { GitBranch, History, Link2, Save, Trash2 } from 'lucide-react';
import { api } from '../lib/api';
import SidePanel from './SidePanel';
import Button from './Button';

const TABS = ['Overview', 'Fields', 'Connections', 'Lineage', 'History', 'Raw'];

function JsonBlock({ value }) {
  return <pre className="overflow-x-auto rounded-lg border border-border bg-background p-3 text-[11px] leading-5 text-muted">{JSON.stringify(value, null, 2)}</pre>;
}

function Empty({ children }) {
  return <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-sm text-muted">{children}</div>;
}

function Field({ label, value, onChange, multiline = false }) {
  const Tag = multiline ? 'textarea' : 'input';
  return (
    <label className="block">
      <span className="mb-1.5 block text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">{label}</span>
      <Tag value={value ?? ''} onChange={(event) => onChange(event.target.value)} rows={multiline ? 5 : undefined}
        className="w-full rounded-lg border border-border bg-background px-3 py-2.5 text-sm text-text outline-none focus:border-accent" />
    </label>
  );
}

export default function ObjectInspector({ record, onClose, onChanged, onRemoved }) {
  const [tab, setTab] = useState('Overview');
  const [details, setDetails] = useState({ history: [], edges: [], events: [], incoming: [], outgoing: [] });
  const [draft, setDraft] = useState(record.data);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    setTab('Overview');
    setDraft(record.data);
    setDetails({ history: [], edges: [], events: [], incoming: [], outgoing: [] });
    const lineage = api.get(`/lineage/${record.kind}/${record.id}`, undefined, record.agentId).catch(() => ({ incoming: [], outgoing: [] }));
    if (record.kind !== 'entity') {
      lineage.then((graph) => setDetails((current) => ({ ...current, ...graph })));
      return;
    }
    Promise.all([
      api.get(`/entity/${record.id}/history`, undefined, record.agentId).then((r) => r.results),
      api.get(`/entity/${record.id}/edges`, undefined, record.agentId).then((r) => r.results),
      api.get(`/entity/${record.id}/events`, undefined, record.agentId).then((r) => r.results),
      lineage,
    ]).then(([history, edges, events, graph]) => setDetails({ history, edges, events, ...graph })).catch(() => {});
  }, [record]);

  const editable = record.kind === 'memory' || record.kind === 'entity' || record.kind === 'episode';

  const save = async () => {
    setSaving(true);
    setError('');
    try {
      let next;
      if (record.kind === 'memory') {
        const result = await api.patch(`/memory/${record.id}`, {
          text: draft.text,
          memory_type: draft.memory_type,
          confidence: draft.confidence,
          tags: Array.isArray(draft.tags) ? draft.tags : String(draft.tags || '').split(',').map((v) => v.trim()).filter(Boolean),
        }, undefined, record.agentId);
        next = result.memory;
      } else if (record.kind === 'entity') {
        const result = await api.patch(`/entity/${record.id}`, {
          name: draft.name,
          description: draft.description,
          agent_summary: draft.agent_summary,
          agent_notes: draft.agent_notes,
          importance_level: draft.importance_level,
          tags: Array.isArray(draft.tags) ? draft.tags : String(draft.tags || '').split(',').map((v) => v.trim()).filter(Boolean),
          attributes: typeof draft.attributes === 'string' ? JSON.parse(draft.attributes || '{}') : draft.attributes,
          change_reason: 'Edited from database workspace',
          triggered_by: 'user',
        }, undefined, record.agentId);
        next = result.entity;
      } else {
        const result = await api.patch(`/lineage/episodes/${record.id}`, {
          title: draft.title,
          summary: draft.summary,
          episode_type: draft.episode_type,
          status: draft.status,
          confidence: Number(draft.confidence),
          tags: Array.isArray(draft.tags) ? draft.tags : String(draft.tags || '').split(',').map((v) => v.trim()).filter(Boolean),
          occurred_start: draft.occurred_start,
          occurred_end: draft.occurred_end,
        }, undefined, record.agentId);
        next = result.episode;
      }
      setDraft(next);
      onChanged(record, next);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!window.confirm(`Remove this ${record.kind}? This action is recorded by the service where supported.`)) return;
    const path = record.kind === 'episode' ? `/lineage/episodes/${record.id}` : `/${record.kind === 'memory' ? 'memory' : 'entity'}/${record.id}`;
    await api.del(path, undefined, record.agentId);
    onRemoved(record);
  };

  const renderOverview = () => (
    <div className="space-y-5">
      <div>
        <div className="mb-2 flex items-center gap-2">
          <span className={`object-kind kind-${record.kind}`}>{record.kind}</span>
          <span className="text-xs text-muted">{record.id}</span>
        </div>
        <h3 className="text-xl font-medium leading-snug text-text">{record.title}</h3>
        {record.subtitle && <p className="mt-2 text-sm leading-6 text-muted">{record.subtitle}</p>}
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="inspector-stat"><span>Confidence</span><strong>{record.confidence}</strong></div>
        <div className="inspector-stat"><span>Agent</span><strong>{record.agentId || 'system'}</strong></div>
        <div className="inspector-stat"><span>Created</span><strong>{record.created ? new Date(record.created).toLocaleDateString() : 'unknown'}</strong></div>
        <div className="inspector-stat"><span>Updated</span><strong>{record.updated ? new Date(record.updated).toLocaleDateString() : 'unchanged'}</strong></div>
      </div>
      <div>
        <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">Why this exists</h4>
        <p className="rounded-lg border border-border bg-background p-3 text-sm leading-6 text-text/80">
          {record.kind === 'evidence' ? 'Immutable source material captured by an ingestion source.' : record.kind === 'analysis' ? 'A processing result derived from one or more evidence objects.' : record.kind === 'entity' ? 'A stable world object that gathers attributes, events, relationships, and changing knowledge.' : record.kind === 'memory' ? 'A durable data object available to memory retrieval.' : 'A structured object produced or used by the memory pipeline.'}
        </p>
      </div>
    </div>
  );

  const renderFields = () => {
    if (!editable) return <JsonBlock value={draft} />;
    if (record.kind === 'memory') return (
      <div className="space-y-4">
        <Field label="Memory text" value={draft.text} onChange={(text) => setDraft({ ...draft, text })} multiline />
        <Field label="Type" value={draft.memory_type} onChange={(memory_type) => setDraft({ ...draft, memory_type })} />
        <Field label="Confidence" value={draft.confidence} onChange={(confidence) => setDraft({ ...draft, confidence })} />
        <Field label="Tags" value={Array.isArray(draft.tags) ? draft.tags.join(', ') : draft.tags} onChange={(tags) => setDraft({ ...draft, tags })} />
      </div>
    );
    if (record.kind === 'episode') return (
      <div className="space-y-4">
        <Field label="Title" value={draft.title} onChange={(title) => setDraft({ ...draft, title })} />
        <Field label="Summary" value={draft.summary} onChange={(summary) => setDraft({ ...draft, summary })} multiline />
        <Field label="Episode type" value={draft.episode_type} onChange={(episode_type) => setDraft({ ...draft, episode_type })} />
        <Field label="Status" value={draft.status} onChange={(status) => setDraft({ ...draft, status })} />
        <Field label="Confidence (0-1)" value={draft.confidence} onChange={(confidence) => setDraft({ ...draft, confidence })} />
        <Field label="Tags" value={Array.isArray(draft.tags) ? draft.tags.join(', ') : draft.tags} onChange={(tags) => setDraft({ ...draft, tags })} />
      </div>
    );
    return (
      <div className="space-y-4">
        <Field label="Name" value={draft.name} onChange={(name) => setDraft({ ...draft, name })} />
        <Field label="Description" value={draft.description} onChange={(description) => setDraft({ ...draft, description })} multiline />
        <Field label="Entity summary" value={draft.agent_summary} onChange={(agent_summary) => setDraft({ ...draft, agent_summary })} multiline />
        <Field label="Internal notes" value={draft.agent_notes} onChange={(agent_notes) => setDraft({ ...draft, agent_notes })} multiline />
        <Field label="Importance" value={draft.importance_level} onChange={(importance_level) => setDraft({ ...draft, importance_level })} />
        <Field label="Tags" value={Array.isArray(draft.tags) ? draft.tags.join(', ') : draft.tags} onChange={(tags) => setDraft({ ...draft, tags })} />
        <Field label="Attributes JSON" value={typeof draft.attributes === 'string' ? draft.attributes : JSON.stringify(draft.attributes, null, 2)} onChange={(attributes) => setDraft({ ...draft, attributes })} multiline />
      </div>
    );
  };

  const content = {
    Overview: renderOverview(),
    Fields: renderFields(),
    Connections: record.kind === 'entity' && details.edges.length ? <div className="space-y-2">{details.edges.map((edge) => <div key={edge.id} className="lineage-row"><Link2 size={15} /><div><strong>{edge.relationship_type}</strong><span>{edge.from_entity_id} to {edge.to_entity_id}</span></div><b>{Math.round(edge.strength * 100)}%</b></div>)}</div> : <Empty>No entity-to-entity connections are stored for this object.</Empty>,
    Lineage: details.incoming.length || details.outgoing.length ? <div className="space-y-5">{details.incoming.length > 0 && <div><h4 className="mb-2 text-xs font-medium text-muted">Inputs</h4><div className="space-y-2">{details.incoming.map((link) => <div key={link.id} className="lineage-row"><GitBranch size={15} /><div><strong>{link.source_type} · {link.relationship}</strong><span>{link.source_id}</span></div><b>{Math.round(link.confidence * 100)}%</b></div>)}</div></div>}{details.outgoing.length > 0 && <div><h4 className="mb-2 text-xs font-medium text-muted">Outputs</h4><div className="space-y-2">{details.outgoing.map((link) => <div key={link.id} className="lineage-row"><GitBranch size={15} /><div><strong>{link.relationship} · {link.target_type}</strong><span>{link.target_id}</span></div><b>{Math.round(link.confidence * 100)}%</b></div>)}</div></div>}</div> : <Empty>No first-class lineage links are stored for this object yet.</Empty>,
    History: record.kind === 'entity' && (details.history.length || details.events.length) ? <div className="space-y-3">{[...details.history.map((h) => ({ ...h, _type: 'change', _at: h.snapshot_at })), ...details.events.map((e) => ({ ...e, _type: 'event', _at: e.occurred_at }))].sort((a, b) => new Date(b._at) - new Date(a._at)).map((item) => <div key={`${item._type}-${item.id}`} className="history-item"><History size={14} /><div><strong>{item._type === 'event' ? item.event_type : item.changed_field}</strong><p>{item.description || item.change_reason || 'No reason recorded'}</p><span>{item._at ? new Date(item._at).toLocaleString() : 'Unknown time'}</span></div></div>)}</div> : <Empty>No version timeline is available for this object type yet.</Empty>,
    Raw: <JsonBlock value={draft} />,
  }[tab];

  return (
    <SidePanel title={`${record.kind} workspace`} onClose={onClose} width={680}>
      <div className="-mx-5 -mt-4 mb-5 overflow-x-auto border-b border-border px-5">
        <div className="flex min-w-max gap-5">{TABS.map((name) => <button key={name} onClick={() => setTab(name)} className={`inspector-tab ${tab === name ? 'active' : ''}`}>{name}</button>)}</div>
      </div>
      {content}
      {error && <p className="mt-4 rounded-lg bg-red-500/10 p-3 text-sm text-red-400">{error}</p>}
      {editable && <div className="sticky bottom-0 -mx-5 mt-6 flex justify-between border-t border-border bg-surface px-5 py-4"><Button variant="danger" onClick={remove}><Trash2 size={14} /> Remove</Button><Button variant="primary" onClick={save} disabled={saving}><Save size={14} /> {saving ? 'Saving...' : 'Save changes'}</Button></div>}
    </SidePanel>
  );
}
