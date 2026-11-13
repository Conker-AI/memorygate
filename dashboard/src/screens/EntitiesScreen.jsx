import { useEffect, useMemo, useState } from 'react';
import { Plus, ChevronRight, ChevronDown, ArrowRight, ArrowLeftRight } from 'lucide-react';
import { api } from '../lib/api';
import { useAgent } from '../context/AgentContext';
import { scopeAgentIds } from '../lib/agentScope';
import { timeAgo } from '../lib/timeAgo';
import EntityGraph from '../components/EntityGraph';
import EntityTypeBadge from '../components/EntityTypeBadge';
import SidePanel from '../components/SidePanel';
import Button from '../components/Button';
import { TextArea } from '../components/TextField';
import { AddConnectionModal, AddEntityModal, AddEventModal, IMPORTANCE_LEVELS } from '../components/EntityModals';
import AgentDot from '../components/AgentDot';


async function loadEntitiesAndEdges(agentIds) {
  const perAgent = await Promise.all(agentIds.map((id) => api.get('/entity', undefined, id)));
  const entities = perAgent.flat();
  const edgePerAgent = await Promise.all(
    agentIds.map((agentId) =>
      Promise.all(entities.filter((e) => e.agent_id === agentId).map((e) => api.get(`/entity/${e.id}/edges`, undefined, agentId).then((r) => r.results))),
    ),
  );
  // each edge is returned by both its "from" and "to" entity's /edges call - dedupe by id
  const seen = new Map();
  edgePerAgent.flat(2).forEach((edge) => seen.set(edge.id, edge));
  return { entities, edges: [...seen.values()] };
}

export default function EntitiesScreen() {
  const { agentId, agents, isAll } = useAgent();
  const agentIds = useMemo(() => scopeAgentIds(agentId, agents), [agentId, agents]);

  const [view, setView] = useState('graph');
  const [entities, setEntities] = useState(null);
  const [edges, setEdges] = useState([]);
  const [selected, setSelected] = useState(null);
  const [adding, setAdding] = useState(false);
  const [sortKey, setSortKey] = useState('updated_at');
  const [sortDir, setSortDir] = useState('desc');
  const [page, setPage] = useState(1);

  const load = async () => {
    setEntities(null);
    const { entities: ents, edges: eds } = await loadEntitiesAndEdges(agentIds);
    setEntities(ents);
    setEdges(eds);
  };

  useEffect(() => { load(); setPage(1); }, [agentId]);

  const edgeCountFor = (id) => edges.filter((e) => e.from_entity_id === id || e.to_entity_id === id).length;

  const sortedEntities = useMemo(() => {
    if (!entities) return [];
    const list = [...entities];
    list.sort((a, b) => {
      let av, bv;
      if (sortKey === 'connections') { av = edgeCountFor(a.id); bv = edgeCountFor(b.id); }
      else if (sortKey === 'name') { av = a.name.toLowerCase(); bv = b.name.toLowerCase(); }
      else if (sortKey === 'entity_type') { av = a.entity_type; bv = b.entity_type; }
      else if (sortKey === 'importance_level') { av = a.importance_level; bv = b.importance_level; }
      else if (sortKey === 'agent_id') { av = a.agent_id; bv = b.agent_id; }
      else { av = a.updated_at || ''; bv = b.updated_at || ''; }
      if (av < bv) return sortDir === 'asc' ? -1 : 1;
      if (av > bv) return sortDir === 'asc' ? 1 : -1;
      return 0;
    });
    return list;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entities, sortKey, sortDir, edges]);

  const pageSize = 25;
  const totalPages = Math.max(1, Math.ceil(sortedEntities.length / pageSize));
  const pageEntities = sortedEntities.slice((page - 1) * pageSize, page * pageSize);

  const toggleSort = (key) => {
    if (sortKey === key) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    else { setSortKey(key); setSortDir('asc'); }
  };

  const openEntity = async (entity) => {
    setSelected(entity);
  };

  const handleUpdated = (updated) => {
    setEntities((prev) => prev.map((e) => (e.id === updated.id ? updated : e)));
    setSelected(updated);
  };

  const handleDeleted = (id) => {
    setEntities((prev) => prev.filter((e) => e.id !== id));
    setEdges((prev) => prev.filter((e) => e.from_entity_id !== id && e.to_entity_id !== id));
    setSelected(null);
  };

  const handleMerge = async (keepId, mergeId) => {
    const keepEntity = entities.find((e) => e.id === keepId);
    const res = await api.post('/entity/merge', { keep_entity_id: keepId, merge_entity_id: mergeId }, undefined, keepEntity.agent_id);
    setEntities((prev) => prev.filter((e) => e.id !== mergeId).map((e) => (e.id === keepId ? res.entity : e)));
    setEdges((prev) => prev
      .filter((e) => !(e.from_entity_id === mergeId && e.to_entity_id === keepId) && !(e.to_entity_id === mergeId && e.from_entity_id === keepId))
      .map((e) => ({
        ...e,
        from_entity_id: e.from_entity_id === mergeId ? keepId : e.from_entity_id,
        to_entity_id: e.to_entity_id === mergeId ? keepId : e.to_entity_id,
      })));
    if (selected?.id === mergeId) setSelected(res.entity);
  };

  return (
    <div className="p-5 md:p-8">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-medium text-text">Entity Graph</h1>
          <p className="mt-1 text-sm text-muted">Structured people, places, projects, and concepts connected into one inspectable memory graph.</p>
        </div>
        <div className="flex gap-1 rounded-lg border border-border bg-surface p-1">
          <button onClick={() => setView('graph')} className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${view === 'graph' ? 'bg-white/[0.08] text-text' : 'text-muted hover:text-text'}`}>Graph</button>
          <button onClick={() => setView('table')} className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${view === 'table' ? 'bg-white/[0.08] text-text' : 'text-muted hover:text-text'}`}>Table</button>
        </div>
      </div>

      {entities === null ? (
        <p className="text-sm text-muted">Loading…</p>
      ) : view === 'graph' ? (
        <EntityGraph entities={entities} edges={edges} selectedId={selected?.id} onSelect={openEntity} onMerge={handleMerge} />
      ) : (
        <EntityTable
          entities={pageEntities}
          isAll={isAll}
          edgeCountFor={edgeCountFor}
          sortKey={sortKey}
          sortDir={sortDir}
          onSort={toggleSort}
          onSelect={openEntity}
          page={page}
          totalPages={totalPages}
          onPage={setPage}
        />
      )}

      <button
        onClick={() => setAdding(true)}
        className="fixed bottom-20 right-5 z-10 flex h-12 w-12 items-center justify-center rounded-full bg-accent text-white transition-colors hover:bg-accent-hover md:bottom-8 md:right-8"
      >
        <Plus size={22} />
      </button>

      {selected && (
        <EntityDetailPanel
          entity={selected}
          agentIds={agentIds}
          onClose={() => setSelected(null)}
          onUpdated={handleUpdated}
          onDeleted={handleDeleted}
          onNavigate={(id) => {
            const target = entities.find((e) => e.id === id);
            if (target) setSelected(target);
          }}
        />
      )}

      {adding && (
        <AddEntityModal
          defaultAgent={isAll ? agentIds[0] : agentId}
          onClose={() => setAdding(false)}
          onCreated={(e) => { setEntities((prev) => [e, ...prev]); setAdding(false); }}
        />
      )}
    </div>
  );
}

function SortHeader({ label, sortKey: key, active, dir, onSort }) {
  return (
    <button onClick={() => onSort(key)} className="flex items-center gap-1 text-left text-xs font-medium uppercase tracking-wide text-muted hover:text-text">
      {label}
      {active && (dir === 'asc' ? <ChevronRight size={12} className="-rotate-90" /> : <ChevronDown size={12} />)}
    </button>
  );
}

function EntityTable({ entities, isAll, edgeCountFor, sortKey, sortDir, onSort, onSelect, page, totalPages, onPage }) {
  return (
    <div>
      <div className="overflow-x-auto rounded-lg border border-border bg-surface">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border">
              <th className="px-4 py-3"><SortHeader label="Name" sortKey="name" active={sortKey === 'name'} dir={sortDir} onSort={onSort} /></th>
              <th className="px-4 py-3"><SortHeader label="Type" sortKey="entity_type" active={sortKey === 'entity_type'} dir={sortDir} onSort={onSort} /></th>
              <th className="px-4 py-3"><SortHeader label="Importance" sortKey="importance_level" active={sortKey === 'importance_level'} dir={sortDir} onSort={onSort} /></th>
              {isAll && <th className="px-4 py-3"><SortHeader label="Agent" sortKey="agent_id" active={sortKey === 'agent_id'} dir={sortDir} onSort={onSort} /></th>}
              <th className="px-4 py-3"><SortHeader label="Connections" sortKey="connections" active={sortKey === 'connections'} dir={sortDir} onSort={onSort} /></th>
              <th className="px-4 py-3"><SortHeader label="Updated" sortKey="updated_at" active={sortKey === 'updated_at'} dir={sortDir} onSort={onSort} /></th>
            </tr>
          </thead>
          <tbody>
            {entities.map((e) => (
              <tr key={e.id} onClick={() => onSelect(e)} className="cursor-pointer border-b border-border last:border-0 hover:bg-white/[0.03]">
                <td className="px-4 py-3 font-medium text-text">{e.name}</td>
                <td className="px-4 py-3"><EntityTypeBadge type={e.entity_type} /></td>
                <td className="px-4 py-3 capitalize text-muted">{e.importance_level}</td>
                {isAll && <td className="px-4 py-3"><AgentDot agentId={e.agent_id} showLabel /></td>}
                <td className="px-4 py-3 text-muted">{edgeCountFor(e.id)}</td>
                <td className="px-4 py-3 text-muted">{timeAgo(e.updated_at)}</td>
              </tr>
            ))}
            {entities.length === 0 && (
              <tr><td colSpan={6} className="px-4 py-8 text-center text-sm text-muted">No entities found.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <div className="mt-3 flex items-center justify-center gap-3 text-xs text-muted">
          <button disabled={page <= 1} onClick={() => onPage(page - 1)} className="rounded-md border border-border px-2.5 py-1 disabled:opacity-40">Previous</button>
          <span>Page {page} of {totalPages}</span>
          <button disabled={page >= totalPages} onClick={() => onPage(page + 1)} className="rounded-md border border-border px-2.5 py-1 disabled:opacity-40">Next</button>
        </div>
      )}
    </div>
  );
}

function EntityDetailPanel({ entity, agentIds, onClose, onUpdated, onDeleted, onNavigate }) {
  const [tab, setTab] = useState('connections');
  const [name, setName] = useState(entity.name);
  const [description, setDescription] = useState(entity.description);
  const [agentNotes, setAgentNotes] = useState(entity.agent_notes);
  const [agentSummary, setAgentSummary] = useState(entity.agent_summary);
  const [importance, setImportance] = useState(entity.importance_level);
  const [tags, setTags] = useState(entity.tags.join(', '));
  const [attrsText, setAttrsText] = useState(JSON.stringify(entity.attributes, null, 2));
  const [attrsOpen, setAttrsOpen] = useState(false);
  const [attrsError, setAttrsError] = useState('');
  const [saving, setSaving] = useState(false);

  const [edgesList, setEdgesList] = useState(null);
  const [events, setEvents] = useState(null);
  const [history, setHistory] = useState(null);
  const [addingConn, setAddingConn] = useState(false);
  const [addingEvent, setAddingEvent] = useState(false);
  const [entityNames, setEntityNames] = useState({});

  useEffect(() => {
    api.get(`/entity/${entity.id}/edges`, undefined, entity.agent_id).then(async (r) => {
      setEdgesList(r.results);
      const ids = [...new Set(r.results.flatMap((e) => [e.from_entity_id, e.to_entity_id]))].filter((id) => id !== entity.id);
      const names = {};
      await Promise.all(ids.map(async (id) => {
        try { const e = await api.get(`/entity/${id}`, undefined, entity.agent_id); names[id] = e.name; } catch { names[id] = id; }
      }));
      setEntityNames(names);
    });
  }, [entity.id]);

  useEffect(() => {
    if (tab === 'events' && events === null) api.get(`/entity/${entity.id}/events`, undefined, entity.agent_id).then((r) => setEvents(r.results));
    if (tab === 'history' && history === null) api.get(`/entity/${entity.id}/history`, undefined, entity.agent_id).then((r) => setHistory(r.results));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  useEffect(() => {
    try { JSON.parse(attrsText || '{}'); setAttrsError(''); } catch { setAttrsError('Invalid JSON'); }
  }, [attrsText]);

  const dirty = name !== entity.name || description !== entity.description || agentNotes !== entity.agent_notes
    || agentSummary !== entity.agent_summary || importance !== entity.importance_level
    || tags !== entity.tags.join(', ') || attrsText !== JSON.stringify(entity.attributes, null, 2);

  const save = async () => {
    if (attrsError) return;
    setSaving(true);
    try {
      const res = await api.patch(`/entity/${entity.id}`, {
        name,
        description,
        agent_notes: agentNotes,
        agent_summary: agentSummary,
        importance_level: importance,
        tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
        attributes: JSON.parse(attrsText || '{}'),
      }, undefined, entity.agent_id);
      onUpdated(res.entity);
    } finally {
      setSaving(false);
    }
  };

  const del = async () => {
    await api.del(`/entity/${entity.id}`, undefined, entity.agent_id);
    onDeleted(entity.id);
  };

  return (
    <SidePanel title="Entity detail" onClose={onClose} width={420}>
      <div className="flex flex-col gap-4">
        <input value={name} onChange={(e) => setName(e.target.value)}
          className="rounded-lg border border-transparent bg-transparent px-0 text-lg font-medium text-text outline-none transition-colors focus-visible:border-accent focus-visible:bg-background focus-visible:px-2 focus-visible:py-1" />

        <div className="flex items-center gap-2">
          <EntityTypeBadge type={entity.entity_type} />
          <AgentDot agentId={entity.agent_id} showLabel />
        </div>

        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-muted">Importance level</span>
          <select value={importance} onChange={(e) => setImportance(e.target.value)}
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-text outline-none focus-visible:border-accent">
            {IMPORTANCE_LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </label>

        <TextArea label="Description" value={description} onChange={(e) => setDescription(e.target.value)} rows={2} />
        <TextArea label="Agent notes" value={agentNotes} onChange={(e) => setAgentNotes(e.target.value)} rows={3} />
        <TextArea label="Agent summary" value={agentSummary} onChange={(e) => setAgentSummary(e.target.value)} rows={2} />

        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-muted">Tags (comma separated)</span>
          <input value={tags} onChange={(e) => setTags(e.target.value)}
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-text outline-none focus-visible:border-accent" />
        </label>

        <div>
          <button onClick={() => setAttrsOpen((o) => !o)} className="flex items-center gap-1.5 text-xs font-medium text-muted hover:text-text">
            {attrsOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />} Attributes (JSON)
          </button>
          {attrsOpen && (
            <div className="mt-2">
              <textarea
                value={attrsText}
                onChange={(e) => setAttrsText(e.target.value)}
                rows={6}
                spellCheck={false}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 font-mono text-xs text-text outline-none focus-visible:border-accent"
              />
              {attrsError && <p className="mt-1 text-xs text-red-400">{attrsError}</p>}
            </div>
          )}
        </div>

        <div className="flex gap-1 border-b border-border pt-2">
          {['connections', 'events', 'history'].map((t) => (
            <button key={t} onClick={() => setTab(t)}
              className={`rounded-t-md px-2.5 py-1.5 text-xs font-medium capitalize transition-colors ${tab === t ? 'border-b-2 border-accent text-accent' : 'text-muted hover:text-text'}`}>
              {t}
            </button>
          ))}
        </div>

        {tab === 'connections' && (
          <div className="flex flex-col gap-3">
            {edgesList === null ? (
              <p className="text-sm text-muted">Loading…</p>
            ) : edgesList.length === 0 ? (
              <p className="text-sm text-muted">No connections yet.</p>
            ) : (
              edgesList.map((edge) => {
                const outgoing = edge.from_entity_id === entity.id;
                const otherId = outgoing ? edge.to_entity_id : edge.from_entity_id;
                return (
                  <div key={edge.id} className="rounded-lg border border-border p-3">
                    <div className="flex items-center gap-2 text-sm">
                      {edge.direction === 'bidirectional' ? <ArrowLeftRight size={13} className="text-muted" /> : <ArrowRight size={13} className={`text-muted ${outgoing ? '' : 'rotate-180'}`} />}
                      <span className="text-muted">{edge.relationship_type}</span>
                      <button onClick={() => onNavigate(otherId)} className="font-medium text-accent hover:underline">
                        {entityNames[otherId] || '…'}
                      </button>
                    </div>
                    <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-white/5">
                      <div className="h-full rounded-full bg-accent" style={{ width: `${edge.strength * 100}%` }} />
                    </div>
                  </div>
                );
              })
            )}
            <Button variant="secondary" onClick={() => setAddingConn(true)}><Plus size={14} /> Add connection</Button>
          </div>
        )}

        {tab === 'events' && (
          <div className="flex flex-col gap-3">
            {events === null ? (
              <p className="text-sm text-muted">Loading…</p>
            ) : events.length === 0 ? (
              <p className="text-sm text-muted">No events yet.</p>
            ) : (
              events.map((ev) => (
                <div key={ev.id} className="rounded-lg border border-border p-3">
                  <div className="flex items-center justify-between">
                    <span className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted">{ev.event_type}</span>
                    <span className="text-[11px] text-muted">{ev.occurred_at || timeAgo(ev.created_at)}</span>
                  </div>
                  <p className="mt-2 text-sm text-text/90">{ev.description}</p>
                  <div className="mt-2 flex gap-0.5">
                    {Array.from({ length: 10 }).map((_, i) => (
                      <span key={i} className={`h-1.5 w-1.5 rounded-full ${i < ev.emotional_weight ? 'bg-accent' : 'bg-white/10'}`} />
                    ))}
                  </div>
                </div>
              ))
            )}
            <Button variant="secondary" onClick={() => setAddingEvent(true)}><Plus size={14} /> Add event</Button>
          </div>
        )}

        {tab === 'history' && (
          <div className="flex flex-col gap-3">
            {history === null ? (
              <p className="text-sm text-muted">Loading…</p>
            ) : history.length === 0 ? (
              <p className="text-sm text-muted">No history yet.</p>
            ) : (
              history.map((h) => (
                <div key={h.id} className="border-l border-border pl-3 text-xs">
                  <p className="font-medium text-text">{h.changed_field}</p>
                  <p className="text-muted">{h.change_reason} · {h.triggered_by} · {timeAgo(h.snapshot_at)}</p>
                </div>
              ))
            )}
          </div>
        )}

        <div className="mt-2 flex justify-between gap-2 border-t border-border pt-4">
          <Button variant="danger" onClick={del}>Delete</Button>
          <Button variant="primary" disabled={!dirty || saving || !!attrsError} onClick={save}>
            {saving ? 'Saving…' : 'Save changes'}
          </Button>
        </div>
      </div>

      {addingConn && (
        <AddConnectionModal
          fromEntity={entity}
          agentIds={agentIds}
          onClose={() => setAddingConn(false)}
          onCreated={(edge, otherName) => {
            setEdgesList((prev) => [...prev, edge]);
            setEntityNames((prev) => ({ ...prev, [edge.to_entity_id === entity.id ? edge.from_entity_id : edge.to_entity_id]: otherName }));
            setAddingConn(false);
          }}
        />
      )}

      {addingEvent && (
        <AddEventModal
          entity={entity}
          onClose={() => setAddingEvent(false)}
          onCreated={(ev) => { setEvents((prev) => [ev, ...(prev || [])]); setAddingEvent(false); }}
        />
      )}
    </SidePanel>
  );
}
