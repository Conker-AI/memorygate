import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Bot, Loader2, Play, RefreshCw, RotateCcw, Search, ShieldAlert } from 'lucide-react';
import { api } from '../lib/api';
import { useAgent } from '../context/AgentContext';
import Button from '../components/Button';
import ModelChoiceNote from '../components/ModelChoiceNote';
import { stageLabel, useModelChoice } from '../lib/modelChoice';

export default function RuntimeScreen() {
  const { agentId } = useAgent();
  const [jobs, setJobs] = useState(null);
  const [query, setQuery] = useState('');
  const [context, setContext] = useState(null);
  const [busy, setBusy] = useState(false);
  const [choice] = useModelChoice();

  const load = () => api.get('/runtime/jobs', { limit: 100 }, agentId).then((r) => setJobs(r.results));
  useEffect(() => { setJobs(null); load(); }, [agentId]);

  const search = async (event) => {
    event.preventDefault(); if (!query.trim()) return; setBusy(true);
    try { setContext(await api.post('/runtime/context', { query, max_items: 12, include_evidence: true }, undefined, agentId)); }
    finally { setBusy(false); }
  };
  const retry = async (id) => { await api.post(`/runtime/jobs/${id}/retry`, {}, undefined, agentId); load(); };
  const failed = (jobs || []).filter((job) => job.status === 'failed');

  return <div className="p-5 md:p-8">
    <div className="mb-6 flex items-end justify-between"><div><h1 className="text-lg font-medium text-text">Agent Runtime</h1><p className="mt-1 text-sm text-muted">Test exactly what your agent retrieves and inspect automatic processing work.</p></div><Button variant="secondary" onClick={load}><RefreshCw size={14} /> Refresh</Button></div>
    <section className="mb-8"><h2 className="mb-1 text-sm font-medium text-text">Context query</h2><p className="mb-3 text-xs text-muted">This calls the same bounded retrieval endpoint intended for Hermes or ToolGate.</p><form onSubmit={search} className="flex rounded-lg border border-border bg-surface p-1.5"><Search size={16} className="mx-2 self-center text-muted" /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="What should the agent remember right now?" className="min-w-0 flex-1 bg-transparent px-1 text-sm text-text outline-none" /><Button type="submit" variant="primary" disabled={busy || !query.trim()}>{busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />} Run</Button></form>
      {context && <div className="mt-3 grid gap-3 lg:grid-cols-3"><Result title="Memories" rows={context.memories} render={(row) => row.text} /><Result title="Entities" rows={context.entities} render={(row) => `${row.name} · ${row.summary || row.description}`} /><Result title="Episodes & evidence" rows={[...context.episodes, ...context.evidence]} render={(row) => row.title || row.summary} /></div>}
    </section>
    <section className="mb-8 rounded-lg border border-border bg-surface p-4"><h2 className="mb-1 text-sm font-medium text-text">Ask about your memory</h2><p className="mb-3 text-xs text-muted">Use Memory Lab for saved, independent read-only questions. It does not create a chat history or give {choice?.managed_by === 'memorygate' ? 'Ollama' : 'the model'} any write, delete, or tool access.</p><ModelChoiceNote choice={choice} className="mb-3" /><Link to="/lab" className="icon-action inline-flex">Open Memory Lab</Link></section>
    <section><div className="mb-3 flex items-end justify-between"><div><h2 className="text-sm font-medium text-text">Processing jobs</h2><p className="mt-1 text-xs text-muted">Every automatically processed evidence object receives a durable job record.</p></div><span className={`text-xs ${failed.length ? 'text-red-400' : 'text-muted'}`}>{failed.length} quarantined</span></div>
      <div className="overflow-hidden rounded-lg border border-border bg-surface">{jobs === null ? <div className="table-empty"><Loader2 size={14} className="animate-spin" /> Loading jobs...</div> : jobs.length === 0 ? <div className="table-empty">No automatic ingestion jobs yet.</div> : jobs.map((job, index) => <div key={job.id} className={`flex items-center gap-3 px-4 py-3 ${index ? 'border-t border-border' : ''}`}><span className={`rounded-md p-2 ${job.status === 'failed' ? 'bg-red-500/10 text-red-400' : 'bg-emerald-500/10 text-emerald-400'}`}>{job.status === 'failed' ? <ShieldAlert size={14} /> : <Bot size={14} />}</span><div className="min-w-0 flex-1"><p className="text-sm text-text">{stageLabel(job.stage)} · {job.status}</p><p className="truncate text-xs text-muted">Evidence {job.evidence_id}{job.error ? ` · ${job.error}` : ''}</p></div><span className="text-xs text-muted">attempt {job.attempts}</span>{job.status === 'failed' && <Button variant="secondary" onClick={() => retry(job.id)}><RotateCcw size={13} /> Retry</Button>}</div>)}</div>
    </section>
  </div>;
}

function Result({ title, rows, render }) {
  return <div className="rounded-lg border border-border bg-surface"><h3 className="border-b border-border px-3 py-2 text-xs font-medium text-muted">{title} ({rows.length})</h3>{rows.length === 0 ? <p className="p-3 text-xs text-muted">No matches.</p> : rows.slice(0, 8).map((row, index) => <div key={row.id} className={`px-3 py-2 text-xs text-text/85 ${index ? 'border-t border-border' : ''}`}>{render(row)}</div>)}</div>;
}
