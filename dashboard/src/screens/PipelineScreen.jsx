import { useEffect, useMemo, useState } from 'react';
import { Activity, Bot, BrainCircuit, CalendarRange, CheckCircle2, ChevronRight, Clock3, Database, FileInput, Loader2, Pause, Play, Radio, RefreshCw, ShieldCheck } from 'lucide-react';
import { api } from '../lib/api';
import { useAgent } from '../context/AgentContext';
import { scopeAgentIds } from '../lib/agentScope';
import { analysisLabel, answeredBy } from '../lib/modelChoice';

const STAGE_META = {
  input: { label: 'Input', icon: FileInput, color: '#22d3ee' },
  evidence: { label: 'Evidence', icon: ShieldCheck, color: '#38bdf8' },
  episode: { label: 'Episode', icon: CalendarRange, color: '#818cf8' },
  analysis: { label: 'Analysis', icon: BrainCircuit, color: '#f59e0b' },
  knowledge: { label: 'Knowledge', icon: Database, color: '#34d399' },
  response: { label: 'Write decision', icon: Bot, color: '#a78bfa' },
};

async function safe(promise, fallback = []) { try { return await promise; } catch { return fallback; } }

function event(stage, item, title, detail, at, agentId) {
  return { id: `${stage}-${item.id}`, stage, item, title, detail, at, agentId };
}

export default function PipelineScreen() {
  const { agentId, agents } = useAgent();
  const agentIds = useMemo(() => scopeAgentIds(agentId, agents), [agentId, agents]);
  const [events, setEvents] = useState(null);
  const [selected, setSelected] = useState(null);
  const [live, setLive] = useState(true);
  const [lastSync, setLastSync] = useState(null);
  const [loading, setLoading] = useState(false);
  const [lineage, setLineage] = useState(null);

  const load = async () => {
    setLoading(true);
    const [evidence, analyses, audit, ...agentData] = await Promise.all([
      Promise.all(agentIds.map((id) => safe(api.get('/evidence', { limit: 100 }, id).then((r) => r.results)))).then((rows) => rows.flat()),
      Promise.all(agentIds.map((id) => safe(api.get('/evidence/analysis', { limit: 100 }, id).then((r) => r.results)))).then((rows) => rows.flat()),
      safe(api.get('/audit')),
      ...agentIds.map(async (id) => ({
        id,
        transcripts: await safe(api.get(`/transcripts/${id}`, undefined, id).then((r) => r.results)),
        episodes: await safe(api.get('/lineage/episodes', { limit: 100 }, id).then((r) => r.results)),
        observations: await safe(api.post('/observation/search', {}, undefined, id).then((r) => r.results)),
        patterns: await safe(api.post('/pattern/search', {}, undefined, id).then((r) => r.results)),
        memories: await safe(api.get('/memory', undefined, id)),
      })),
    ]);
    const next = [];
    agentData.forEach((group) => {
      group.transcripts.forEach((v) => next.push(event('input', v, `Session ${v.session_id}`, `${v.word_count} words received`, v.created_at || v.session_start, group.id)));
      group.episodes.forEach((v) => next.push(event('episode', v, v.title, `${v.episode_type} · ${v.status} · ${Math.round(v.confidence * 100)}%`, v.occurred_start || v.created_at, group.id)));
      group.observations.forEach((v) => next.push(event('analysis', v, v.description, `${v.signal_type} observation · ${v.status}`, v.observed_at, group.id)));
      group.patterns.forEach((v) => next.push(event('knowledge', v, v.pattern_name, `${v.status} pattern from ${v.observation_ids.length} observations`, v.updated_at || v.created_at, group.id)));
      group.memories.forEach((v) => next.push(event('knowledge', v, v.text, `${v.memory_type} · ${v.confidence} confidence`, v.updated_at || v.created_at, group.id)));
    });
    evidence.forEach((v) => next.push(event('evidence', v, v.title, `${v.source_type} · integrity ${Math.round(v.integrity_confidence * 100)}%`, v.created_at || v.occurred_at)));
    analyses.forEach((v) => {
      const by = answeredBy(v.model, v.provider);
      next.push(event('analysis', v, v.output_summary || analysisLabel(v.analysis_type), `${v.evidence_ids.length} evidence inputs · ${Math.round(v.confidence * 100)}%${by ? ` · by ${by}` : ''}`, v.created_at));
    });
    audit.filter((v) => {
      if (agentId === 'all') return true;
      try { return JSON.parse(v.payload_json || '{}').agent_id === agentId; } catch { return false; }
    }).slice(0, 100).forEach((v) => {
      let payload = {}; try { payload = JSON.parse(v.payload_json || '{}'); } catch { /* raw payload remains inspectable */ }
      next.push(event('response', { ...v, payload }, `${v.action} operation`, payload.text || payload.reason || `Memory operation ${v.memory_id || ''}`, v.created_at, payload.agent_id));
    });
    next.sort((a, b) => new Date(b.at || 0) - new Date(a.at || 0));
    setEvents(next);
    setSelected((current) => current ? next.find((v) => v.id === current.id) || current : next[0] || null);
    setLastSync(new Date());
    setLoading(false);
  };

  useEffect(() => { load(); }, [agentId]);
  useEffect(() => {
    if (!live) return undefined;
    const timer = window.setInterval(load, 5000);
    return () => window.clearInterval(timer);
  }, [live, agentId]);
  useEffect(() => {
    if (!selected) { setLineage(null); return; }
    setLineage(null);
    api.get(`/lineage/${selected.stage === 'input' ? 'transcript' : selected.stage === 'response' ? 'memory' : selected.stage}/${selected.item.id}`)
      .then(setLineage)
      .catch(() => setLineage({ incoming: [], outgoing: [] }));
  }, [selected]);

  const stages = Object.keys(STAGE_META);
  const stageCounts = useMemo(() => Object.fromEntries(stages.map((stage) => [stage, (events || []).filter((v) => v.stage === stage).length])), [events]);
  const recent = (events || []).slice(0, 60);

  return (
    <div className="workspace-page pipeline-page">
      <header className="workspace-header">
        <div><span className="eyebrow">Runtime observability</span><h1>Live Pipeline</h1><p>Watch information move from raw input into evidence, interpretation, durable knowledge, and recorded write decisions.</p></div>
        <div className="header-actions"><button className={`live-toggle ${live ? 'active' : ''}`} onClick={() => setLive((v) => !v)}>{live ? <Pause size={14} /> : <Play size={14} />} {live ? 'Live' : 'Paused'}</button><button className="icon-action" onClick={load}><RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> Sync</button></div>
      </header>

      <div className="pipeline-status">
        <div><span className="pulse-dot" /><strong>Collector online</strong><small>{lastSync ? `synced ${lastSync.toLocaleTimeString()}` : 'connecting'}</small></div>
        <div><Activity size={16} /><strong>{recent.length}</strong><small>recent operations</small></div>
        <div><Clock3 size={16} /><strong>5 sec</strong><small>refresh interval</small></div>
        <div><Radio size={16} /><strong>1</strong><small>local memory stream</small></div>
      </div>

      <section className="pipeline-map">
        {stages.map((stage, index) => {
          const meta = STAGE_META[stage]; const Icon = meta.icon;
          return <div className="pipeline-stage" key={stage} style={{ '--stage-color': meta.color }}><div className="stage-head"><Icon size={17} /><span>0{index + 1}</span></div><strong>{meta.label}</strong><small>{stageCounts[stage]} recorded</small>{index < stages.length - 1 && <ChevronRight className="stage-arrow" size={18} />}</div>;
        })}
      </section>

      <div className="pipeline-workbench">
        <section className="event-stream">
          <div className="panel-title"><div><h2>Event stream</h2><span>Newest operations across every processing stage</span></div>{live && <span className="recording"><i /> recording</span>}</div>
          {!events && <div className="table-empty"><Loader2 size={15} className="animate-spin" /> Connecting to pipeline...</div>}
          {recent.map((item) => { const meta = STAGE_META[item.stage]; const Icon = meta.icon; return <button key={item.id} onClick={() => setSelected(item)} className={`stream-event ${selected?.id === item.id ? 'selected' : ''}`}><span className="event-icon" style={{ color: meta.color, background: `${meta.color}16` }}><Icon size={15} /></span><span className="event-copy"><strong>{item.title}</strong><small>{item.detail}</small></span><span className="event-meta"><b>{meta.label}</b><small>{item.at ? new Date(item.at).toLocaleTimeString() : 'unknown'}</small></span></button>; })}
          {events && recent.length === 0 && <div className="table-empty">No pipeline activity has been recorded yet.</div>}
        </section>

        <aside className="trace-inspector">
          <div className="panel-title"><div><h2>Trace inspector</h2><span>What MemoryGate received and understood</span></div></div>
          {selected ? <>
            <div className="trace-stage" style={{ '--trace-color': STAGE_META[selected.stage].color }}><span>{STAGE_META[selected.stage].label}</span><b>{selected.at ? new Date(selected.at).toLocaleString() : 'No timestamp'}</b></div>
            <h3>{selected.title}</h3><p>{selected.detail}</p>
            <div className="trace-facts"><div><span>Object ID</span><code>{selected.item.id}</code></div><div><span>Source</span><code>{selected.item.source_key || 'local memory'}</code></div><div><span>Processing state</span><code>recorded</code></div></div>
            <h4>Exact lineage</h4>
            {lineage && (lineage.incoming.length || lineage.outgoing.length) ? <div className="mx-[15px] space-y-1.5">{lineage.incoming.map((link) => <div key={link.id} className="lineage-row"><span className="text-[10px] text-muted">{link.source_type} <b className="text-text">{link.relationship}</b> this object</span></div>)}{lineage.outgoing.map((link) => <div key={link.id} className="lineage-row"><span className="text-[10px] text-muted">this object <b className="text-text">{link.relationship}</b> {link.target_type}</span></div>)}</div> : <p>{lineage ? 'No exact links recorded for this object.' : 'Loading lineage...'}</p>}
            <h4>Recorded payload</h4><pre>{JSON.stringify(selected.item, null, 2)}</pre>
          </> : <div className="table-empty">Select an event to inspect its payload.</div>}
        </aside>
      </div>

      <div className="pipeline-note"><CheckCircle2 size={15} /><p><strong>Honest trace mode.</strong> Existing records are ordered by their real timestamps. Stages without explicit lineage IDs are shown as nearby operations, not falsely claimed as one causal chain. Agent response text will appear only after a response listener is connected.</p></div>
    </div>
  );
}
