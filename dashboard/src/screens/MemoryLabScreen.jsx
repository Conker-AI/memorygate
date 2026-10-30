import { useEffect, useState } from 'react';
import { Bot, Clock3, FileSearch, Loader2, Search, Trash2 } from 'lucide-react';
import { api } from '../lib/api';
import { useAgent } from '../context/AgentContext';
import Button from '../components/Button';
import Modal from '../components/Modal';
import ModelChoiceNote from '../components/ModelChoiceNote';
import { answeredBy, useModelChoice } from '../lib/modelChoice';

function historyKey(agentId) {
  return `memorygate_lab_history:${agentId}`;
}

function loadHistory(agentId) {
  try {
    return JSON.parse(sessionStorage.getItem(historyKey(agentId)) || '[]');
  } catch {
    return [];
  }
}

function ObjectCard({ label, object, onOpen }) {
  const title = object.name || object.title || object.text || object.summary || object.id;
  const detail = object.summary || object.description || object.text || '';
  return <button onClick={() => onOpen({ label, object })} className="w-full border-b border-border px-3 py-3 text-left last:border-b-0 hover:bg-white/[0.03]">
    <div className="flex items-center justify-between gap-2"><span className="text-[10px] uppercase tracking-wider text-accent">{label}</span><span className="font-mono text-[10px] text-muted">{object.id?.slice(0, 8)}</span></div>
    <p className="mt-1 text-sm text-text">{title}</p>{detail && detail !== title && <p className="mt-1 line-clamp-2 text-xs text-muted">{detail}</p>}
  </button>;
}

export default function MemoryLabScreen() {
  const { agentId } = useAgent();
  const [question, setQuestion] = useState('');
  const [history, setHistory] = useState(() => loadHistory(agentId));
  const [selectedId, setSelectedId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [inspected, setInspected] = useState(null);
  const [choice, reloadChoice] = useModelChoice();
  // The Ollama wording is true only on the direct path; otherwise Conker chooses the model.
  const direct = choice?.managed_by === 'memorygate';
  const modelName = direct ? 'Ollama' : 'the model';

  useEffect(() => {
    const next = loadHistory(agentId);
    setHistory(next);
    setSelectedId(next[0]?.id || null);
    setQuestion('');
    setError('');
  }, [agentId]);

  const selected = history.find((item) => item.id === selectedId) || history[0] || null;

  async function ask(event) {
    event.preventDefault();
    if (!question.trim()) return;
    setBusy(true); setError('');
    try {
      const result = await api.post('/runtime/ask', { question: question.trim(), include_evidence: true }, undefined, agentId);
      const item = { id: crypto.randomUUID(), createdAt: new Date().toISOString(), ...result };
      const next = [item, ...history].slice(0, 20);
      sessionStorage.setItem(historyKey(agentId), JSON.stringify(next));
      setHistory(next); setSelectedId(item.id); setQuestion('');
      reloadChoice();
    } catch (err) {
      setError(err.message || (direct ? 'Local Ollama could not answer right now.' : 'The model could not answer right now.'));
    } finally {
      setBusy(false);
    }
  }

  function clearHistory() {
    sessionStorage.removeItem(historyKey(agentId));
    setHistory([]); setSelectedId(null);
  }

  const context = selected?.context || { memories: [], entities: [], episodes: [], evidence: [] };
  const objects = [
    ...context.memories.map((object) => ({ label: 'Memory', object })),
    ...context.entities.map((object) => ({ label: 'Entity', object })),
    ...context.episodes.map((object) => ({ label: 'Episode', object })),
    ...context.evidence.map((object) => ({ label: 'Evidence', object })),
  ];

  return <div className="p-5 md:p-8">
    <div className="mb-6"><h1 className="text-lg font-medium text-text">Memory Lab</h1><p className="mt-1 max-w-3xl text-sm text-muted">Ask independent, read-only questions about your memory. Results persist while this browser session is open, but {modelName} never receives prior answers as a conversation.</p></div>
    <form onSubmit={ask} className="rounded-xl border border-border bg-surface p-3"><div className="flex gap-3"><Search size={17} className="mt-2.5 shrink-0 text-muted" /><input value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="Ask what MemoryGate knows..." className="min-w-0 flex-1 bg-transparent py-2 text-sm text-text outline-none" /><Button type="submit" disabled={busy || !question.trim()}>{busy ? <Loader2 size={14} className="animate-spin" /> : <Bot size={14} />}{busy ? 'Checking memory...' : 'Ask MemoryGate'}</Button></div><div className="mt-2 space-y-1.5 border-t border-border pt-2"><p className="text-xs text-muted">{direct ? 'Ollama' : 'The model'} can only answer from retrieved objects. It has no write tools, no deletion tools, and no hidden chat history.</p><ModelChoiceNote choice={choice} /></div></form>
    {error && <p className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</p>}
    <div className="mt-6 grid gap-5 xl:grid-cols-[280px_minmax(0,1fr)]"><section className="overflow-hidden rounded-xl border border-border bg-surface"><div className="flex items-center justify-between border-b border-border px-4 py-3"><div><p className="text-sm font-medium text-text">Investigations</p><p className="text-xs text-muted">This browser session only</p></div>{history.length > 0 && <button onClick={clearHistory} className="rounded p-1 text-muted hover:bg-white/[0.06] hover:text-text" title="Clear session history"><Trash2 size={15} /></button>}</div>{history.length === 0 ? <p className="px-4 py-5 text-sm text-muted">No questions yet.</p> : history.map((item) => <button key={item.id} onClick={() => setSelectedId(item.id)} className={`w-full border-b border-border px-4 py-3 text-left last:border-b-0 ${selected?.id === item.id ? 'bg-accent/10' : 'hover:bg-white/[0.03]'}`}><p className="line-clamp-2 text-sm text-text">{item.question}</p><p className="mt-1 flex items-center gap-1 text-xs text-muted"><Clock3 size={11} /> {new Date(item.createdAt).toLocaleTimeString()}</p></button>)}</section>
      <section className="min-w-0"><div className="rounded-xl border border-border bg-surface p-5">{selected ? <><p className="text-xs font-medium uppercase tracking-wider text-accent">{direct ? 'Read-only local answer' : 'Read-only answer'}</p><h2 className="mt-2 text-base font-medium text-text">{selected.question}</h2><p className="mt-4 whitespace-pre-wrap text-sm leading-7 text-text/90">{selected.answer}</p>{answeredBy(selected.model, selected.provider) && <p className="mt-3 text-xs text-muted">Answered by {answeredBy(selected.model, selected.provider)}</p>}</> : <div className="py-8 text-center text-sm text-muted"><FileSearch className="mx-auto mb-3" size={22} />Run an investigation to inspect the retrieved memory objects.</div>}</div>{selected && <div className="mt-4 overflow-hidden rounded-xl border border-border bg-surface"><div className="border-b border-border px-4 py-3"><p className="text-sm font-medium text-text">Retrieved objects ({objects.length})</p><p className="mt-1 text-xs text-muted">Open any object to inspect the exact data supplied to {modelName}.</p></div>{objects.length ? objects.map(({ label, object }) => <ObjectCard key={`${label}-${object.id}`} label={label} object={object} onOpen={setInspected} />) : <p className="px-4 py-4 text-sm text-muted">No objects matched this question.</p>}</div>}</section></div>
    {inspected && <Modal title={`${inspected.label} object`} onClose={() => setInspected(null)} width="max-w-3xl"><pre className="overflow-x-auto rounded-lg bg-background p-3 text-xs leading-5 text-text">{JSON.stringify(inspected.object, null, 2)}</pre></Modal>}
  </div>;
}
