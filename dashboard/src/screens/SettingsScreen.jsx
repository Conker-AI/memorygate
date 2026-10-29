import { useEffect, useState } from 'react';
import { AlertTriangle, BrainCircuit, Check, Copy, KeyRound, Loader2, RefreshCw, Save, Shield, Trash2 } from 'lucide-react';
import { API_BASE_URL, api, getStoredKey, storeKey } from '../lib/api';
import Button from '../components/Button';
import Modal from '../components/Modal';
import TextField from '../components/TextField';
import { useAuth } from '../context/AuthContext';

function Section({ title, note, children }) {
  return (
    <section className="mb-8">
      <h2 className="mb-1 text-sm font-medium text-text">{title}</h2>
      {note && <p className="mb-3 text-xs text-muted">{note}</p>}
      <div className="overflow-hidden rounded-xl border border-border bg-surface">{children}</div>
    </section>
  );
}

const PASSWORD_RULES = [
  { id: 'length', label: 'At least 14 characters', test: (value) => value.length >= 14 },
  { id: 'lower', label: 'One lowercase letter', test: (value) => /[a-z]/.test(value) },
  { id: 'upper', label: 'One uppercase letter', test: (value) => /[A-Z]/.test(value) },
  { id: 'number', label: 'One number', test: (value) => /\d/.test(value) },
  { id: 'symbol', label: 'One special character', test: (value) => /[^A-Za-z0-9]/.test(value) },
];

function PasswordRules({ value }) {
  return (
    <div className="rounded-lg border border-border bg-background px-3 py-3">
      <p className="mb-2 text-xs font-medium text-text">New key requirements</p>
      <div className="flex flex-col gap-1.5">
        {PASSWORD_RULES.map((rule) => {
          const ok = rule.test(value);
          return (
            <div key={rule.id} className={`text-xs ${ok ? 'text-emerald-300' : 'text-muted'}`}>
              {ok ? '✓' : '•'} {rule.label}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function RotateKeyModal({ onClose, onDone }) {
  const { refreshAuth } = useAuth();
  const [currentKey, setCurrentKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [generatedKey, setGeneratedKey] = useState('');
  const [copied, setCopied] = useState(false);

  async function confirm() {
    setBusy(true);
    setError('');
    try {
      const res = await api.post('/auth/rotate-key', { current_key: currentKey, length: 24 });
      storeKey(res.new_key);
      await refreshAuth();
      setGeneratedKey(res.new_key);
      onDone('Admin key rotated. Copy the new key before closing this modal.');
    } catch (err) {
      setError(err.message || 'Unable to rotate key.');
    } finally {
      setBusy(false);
    }
  }

  async function copyKey() {
    if (!generatedKey) return;
    await navigator.clipboard.writeText(generatedKey);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }

  return (
    <Modal title="Rotate admin key" onClose={onClose}>
      {!generatedKey ? (
        <>
          <p className="text-sm text-muted">Any connected app using the old key will lose access immediately.</p>
          <div className="mt-4">
            <TextField
              label="Current key"
              type="password"
              value={currentKey}
              onChange={(e) => setCurrentKey(e.target.value)}
              placeholder="Current admin key"
              autoFocus
            />
          </div>
          {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="secondary" onClick={onClose}>Cancel</Button>
            <Button variant="danger" onClick={confirm} disabled={busy || !currentKey.trim()}>
              {busy ? <Loader2 size={14} className="animate-spin" /> : 'Rotate'}
            </Button>
          </div>
        </>
      ) : (
        <>
          <div className="rounded-lg border border-border bg-background p-4">
            <div className="mb-2 flex items-center gap-2 text-sm font-medium text-text">
              <KeyRound size={15} className="text-accent" />
              New admin key
            </div>
            <code className="block overflow-x-auto rounded-md bg-surface px-3 py-2 text-xs text-accent">{generatedKey}</code>
          </div>
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="secondary" onClick={copyKey}>
              {copied ? <Check size={14} /> : <Copy size={14} />}
              {copied ? 'Copied' : 'Copy key'}
            </Button>
            <Button onClick={onClose}>Done</Button>
          </div>
        </>
      )}
    </Modal>
  );
}

function ChangeKeyModal({ onClose, onDone }) {
  const { refreshAuth } = useAuth();
  const [currentKey, setCurrentKey] = useState('');
  const [newKey, setNewKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const newKeyValid = PASSWORD_RULES.every((rule) => rule.test(newKey));

  async function confirm() {
    setBusy(true);
    setError('');
    try {
      await api.post('/auth/change-key', { current_key: currentKey, new_key: newKey });
      storeKey(newKey);
      await refreshAuth();
      onDone('Admin key updated. This session is now using the new key.');
      onClose();
    } catch (err) {
      setError(err.message || 'Unable to update key.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Change admin key" onClose={onClose}>
      <div className="space-y-3">
        <TextField
          label="Current key"
          type="password"
          value={currentKey}
          onChange={(e) => setCurrentKey(e.target.value)}
          placeholder="Current admin key"
          autoFocus
        />
        <TextField
          label="New key"
          type="password"
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          placeholder="New admin key"
        />
        <PasswordRules value={newKey} />
      </div>
      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
      <div className="mt-5 flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button onClick={confirm} disabled={busy || !currentKey.trim() || !newKeyValid}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
          {busy ? 'Saving...' : 'Save'}
        </Button>
      </div>
    </Modal>
  );
}

function AgentKeyModal({ onClose, onCreated }) {
  const [label, setLabel] = useState('');
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function create() {
    setBusy(true);
    setError('');
    try {
      const result = await api.post('/auth/agent-keys', { label, agent_id: 'conker' });
      setKey(result.key);
      onCreated();
    } catch (err) {
      setError(err.message || 'Unable to create the read-only key.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Create agent read key" onClose={onClose}>
      {!key ? <>
        <p className="mb-4 text-sm text-muted">This key can only retrieve context for one agent. It cannot ingest, edit, or administer MemoryGate.</p>
        <div className="space-y-3">
          <TextField label="Label" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Hermes production" autoFocus />
        </div>
        {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
        <div className="mt-5 flex justify-end gap-2"><Button variant="secondary" onClick={onClose}>Cancel</Button><Button onClick={create} disabled={busy || !label.trim()}>{busy ? 'Creating...' : 'Create key'}</Button></div>
      </> : <>
        <p className="mb-3 text-sm text-muted">Copy this once. MemoryGate stores only a hash and cannot reveal it again.</p>
        <code className="block overflow-x-auto rounded-md bg-background px-3 py-2 text-xs text-accent">{key}</code>
        <div className="mt-5 flex justify-end gap-2"><Button variant="secondary" onClick={() => navigator.clipboard.writeText(key)}><Copy size={14} /> Copy key</Button><Button onClick={onClose}>Done</Button></div>
      </>}
    </Modal>
  );
}

function AiRuntimeModal({ runtime, onClose, onSaved }) {
  const [provider, setProvider] = useState(runtime?.provider || 'ollama');
  const [model, setModel] = useState(runtime?.model || 'qwen3:4b');
  const [apiKey, setApiKey] = useState('');
  const [currentKey, setCurrentKey] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function save() {
    setBusy(true); setError('');
    try {
      await api.put('/system/ai-runtime', { provider, model, api_key: apiKey || null, clear_api_key: clearKey, current_key: currentKey });
      onSaved(`AI runtime updated to ${provider === 'openai' ? 'OpenAI' : 'Ollama'} / ${model}.`);
      onClose();
    } catch (err) {
      setError(err.message || 'Unable to update the AI runtime.');
    } finally { setBusy(false); }
  }

  return <Modal title="Configure AI runtime" onClose={onClose} width="max-w-xl">
    <p className="mb-4 text-sm text-muted">This model only analyzes evidence and answers bounded Memory Lab questions. It never receives write, delete, or tool access.</p>
    <div className="space-y-4">
      <label className="block"><span className="mb-1.5 block text-xs font-medium text-muted">Provider</span><select value={provider} onChange={(event) => setProvider(event.target.value)} className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-text outline-none"><option value="ollama">Ollama (local)</option><option value="openai">OpenAI API</option></select></label>
      <TextField label="Model" value={model} onChange={(event) => setModel(event.target.value)} placeholder={provider === 'ollama' ? 'qwen3:4b' : 'gpt-5.6-luna'} />
      {provider === 'openai' && <><TextField label={runtime?.api_key_configured ? 'New API key (leave blank to keep the current key)' : 'OpenAI API key'} type="password" value={apiKey} onChange={(event) => { setApiKey(event.target.value); setClearKey(false); }} placeholder="sk-..." /><p className="-mt-2 text-xs text-muted">The key is encrypted on the MemoryGate server and is never returned to this browser.</p>{runtime?.api_key_configured && <label className="flex items-center gap-2 text-xs text-muted"><input type="checkbox" checked={clearKey} onChange={(event) => setClearKey(event.target.checked)} /> Remove the saved API key</label>}</>}
      {provider === 'ollama' && <p className="rounded-lg border border-border bg-background px-3 py-2 text-xs text-muted">Installed models are discovered from your local Ollama server. Enter any installed model name.</p>}
      <TextField label="Current admin key" type="password" value={currentKey} onChange={(event) => setCurrentKey(event.target.value)} placeholder="Required to save" />
    </div>
    {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
    <div className="mt-5 flex justify-end gap-2"><Button variant="secondary" onClick={onClose}>Cancel</Button><Button onClick={save} disabled={busy || !model.trim() || !currentKey.trim() || (provider === 'openai' && !runtime?.api_key_configured && !apiKey.trim())}>{busy ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}{busy ? 'Saving...' : 'Save runtime'}</Button></div>
  </Modal>;
}

function ResetMemoryModal({ mode, onClose, onReset }) {
  const [currentKey, setCurrentKey] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [resetFrom, setResetFrom] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const dated = mode === 'date';
  async function reset() {
    setBusy(true); setError('');
    try {
      const result = await api.post('/system/memory-reset', { current_key: currentKey, confirmation, reset_from: dated ? new Date(resetFrom).toISOString() : null });
      const total = Object.values(result.removed || {}).reduce((sum, count) => sum + count, 0);
      onReset(`Reset complete: ${total} records removed. Backup ${result.backup.filename} was created first.`);
      onClose();
    } catch (err) { setError(err.message || 'Unable to reset MemoryGate.'); }
    finally { setBusy(false); }
  }
  return <Modal title={dated ? 'Reset data from date' : 'Reset all memory'} onClose={onClose} width="max-w-xl">
    <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200"><div className="flex items-center gap-2 font-medium"><AlertTriangle size={16} /> This is destructive.</div><p className="mt-1 text-red-200/80">A logical backup is created first. Listener credentials, read keys, admin access, and AI settings are not removed.</p></div>
    <div className="mt-4 space-y-3">{dated && <TextField label="Delete data created on or after" type="datetime-local" value={resetFrom} onChange={(event) => setResetFrom(event.target.value)} required />}<TextField label="Current admin key" type="password" value={currentKey} onChange={(event) => setCurrentKey(event.target.value)} placeholder="Required for reset" /><TextField label='Type RESET MEMORY to confirm' value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder="RESET MEMORY" /></div>
    {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
    <div className="mt-5 flex justify-end gap-2"><Button variant="secondary" onClick={onClose}>Cancel</Button><Button variant="danger" onClick={reset} disabled={busy || !currentKey.trim() || confirmation !== 'RESET MEMORY' || (dated && !resetFrom)}>{busy ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}{busy ? 'Resetting...' : 'Reset memory'}</Button></div>
  </Modal>;
}

export default function SettingsScreen() {
  const [status, setStatus] = useState(null);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [modal, setModal] = useState(null);
  const [agentKeys, setAgentKeys] = useState([]);
  const [backups, setBackups] = useState([]);
  const [backupBusy, setBackupBusy] = useState(false);
  const [runtime, setRuntime] = useState(null);
  // With Pi configured, MemoryGate's model is chosen in Conker; this screen only says where.
  const piChooses = runtime?.managed_by === 'pi';

  async function loadStatus() {
    const res = await api.get('/auth/settings');
    setStatus(res);
    const keys = await api.get('/auth/agent-keys');
    setAgentKeys(keys.results || []);
    const backupData = await api.get('/system/backups');
    setBackups(backupData.results || []);
    setRuntime(await api.get('/system/ai-runtime'));
  }

  useEffect(() => {
    loadStatus();
  }, []);

  function showSuccess(nextMessage) {
    setError('');
    setMessage(nextMessage);
    loadStatus();
  }

  async function revokeAgentKey(id) {
    try {
      await api.post(`/auth/agent-keys/${id}/revoke`, {});
      showSuccess('Agent read key revoked.');
    } catch (err) {
      setError(err.message || 'Unable to revoke agent key.');
    }
  }

  async function createBackup() {
    setBackupBusy(true);
    try {
      await api.post('/system/backups', {});
      showSuccess('Backup created in the persistent MemoryGate backup volume.');
    } catch (err) {
      setError(err.message || 'Unable to create backup.');
    } finally {
      setBackupBusy(false);
    }
  }

  async function downloadBackup(filename) {
    try {
      const response = await fetch(`${API_BASE_URL}/system/backups/${encodeURIComponent(filename)}/download`, {
        headers: { 'X-MemoryGate-Key': getStoredKey() },
      });
      if (!response.ok) throw new Error('Unable to download backup.');
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err.message || 'Unable to download backup.');
    }
  }

  return (
    <div className="p-5 md:p-8">
      <h1 className="mb-6 text-lg font-medium text-text">Settings</h1>

      {(message || error) && (
        <div className={`mb-6 rounded-xl border px-4 py-3 text-sm ${error ? 'border-red-500/30 bg-red-500/10 text-red-300' : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'}`}>
          {error || message}
        </div>
      )}

      <Section title="Access Keys" note="ToolGate-style key management, adapted for MemoryGate with current-key confirmation on every sensitive action.">
        <div className="flex flex-wrap items-center gap-3 px-4 py-3">
          <span className="w-24 flex-shrink-0 text-sm text-text">Admin Key</span>
          <span className="min-w-0 flex-1 truncate font-mono text-xs text-muted">
            {status === null
              ? 'Loading status...'
              : status.auth_enabled
                ? '••••••••••••••••'
                : 'No admin key set yet'}
          </span>
          <Button variant="secondary" className="!py-1.5 text-xs" onClick={() => { setError(''); setMessage(''); setModal('change'); }}>
            <Save size={13} /> Change
          </Button>
          <Button variant="secondary" className="!py-1.5 text-xs" onClick={() => { setError(''); setMessage(''); setModal('rotate'); }}>
            <RefreshCw size={13} /> Rotate
          </Button>
        </div>
      </Section>

      <Section title="Access status" note="Where the active admin key is coming from right now.">
        <div className="flex items-center gap-3 px-4 py-3">
          <Shield size={16} className="text-accent" />
          <div className="min-w-0 flex-1">
            <p className="text-sm text-text">
              {status === null
                ? 'Loading admin key status...'
                : status.auth_enabled
                  ? 'Admin key protection is enabled.'
                  : 'No admin key is configured yet.'}
            </p>
            <p className="text-xs text-muted">
              {status === null
                ? 'Checking backend status...'
                : status.auth_enabled
                  ? `Source: ${status.key_source}`
                  : 'The first change or rotate action will create the managed admin key.'}
            </p>
          </div>
        </div>
      </Section>

      <Section title="Agent Read Keys" note="Scoped credentials for Hermes, MCP, or any other agent. They can retrieve context only for their assigned agent ID.">
        <div className="flex justify-end border-b border-border px-4 py-3"><Button variant="secondary" className="!py-1.5 text-xs" onClick={() => setModal('agent-key')}><KeyRound size={13} /> Create read key</Button></div>
        {agentKeys.length === 0 ? <p className="px-4 py-3 text-sm text-muted">No agent read keys yet.</p> : agentKeys.map((key, index) => (
          <div key={key.id} className={`flex flex-wrap items-center gap-3 px-4 py-3 ${index ? 'border-t border-border' : ''}`}>
            <div className="min-w-0 flex-1"><p className="text-sm text-text">{key.label}</p><p className="mt-1 font-mono text-xs text-muted">{key.agent_id}{key.last_used_at ? ` · used ${new Date(key.last_used_at).toLocaleString()}` : ''}</p></div>
            {key.revoked ? <span className="text-xs text-muted">revoked</span> : <Button variant="danger" className="!py-1.5 text-xs" onClick={() => revokeAgentKey(key.id)}>Revoke</Button>}
          </div>
        ))}
      </Section>

      <Section title="Backups" note="Logical exports of memory data, lineage, and processing state. Credential hashes and listener secrets are deliberately excluded.">
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3"><span className="text-sm text-muted">{backups.length ? `${backups.length} backup${backups.length === 1 ? '' : 's'} available` : 'No backups yet'}</span><Button variant="secondary" className="!py-1.5 text-xs" onClick={createBackup} disabled={backupBusy}>{backupBusy ? 'Creating...' : 'Create backup'}</Button></div>
        {backups.slice(0, 5).map((backup, index) => <div key={backup.filename} className={`flex items-center gap-3 px-4 py-3 ${index ? 'border-t border-border' : ''}`}><div className="min-w-0 flex-1"><p className="truncate font-mono text-xs text-text">{backup.filename}</p><p className="mt-1 text-xs text-muted">{new Date(backup.created_at).toLocaleString()} · {Math.max(1, Math.round(backup.size / 1024))} KB</p></div><Button variant="secondary" className="!py-1.5 text-xs" onClick={() => downloadBackup(backup.filename)}>Download</Button></div>)}
      </Section>

      <Section title="AI Runtime" note={piChooses ? 'Conker picks this model in one place for every module. Semantic retrieval remains local and unchanged.' : 'Choose the bounded-analysis model. Semantic retrieval remains local and unchanged.'}>
        {piChooses ? <div className="flex flex-wrap items-center gap-3 px-4 py-3"><BrainCircuit size={16} className="text-accent" /><div className="min-w-0 flex-1"><p className="text-sm text-text">{runtime.chosen_in}</p><p className="mt-1 text-xs text-muted">MemoryGate asks Conker's memory sorting role, so the model is changed there, not here.</p></div></div> : <div className="flex flex-wrap items-center gap-3 px-4 py-3"><BrainCircuit size={16} className="text-accent" /><div className="min-w-0 flex-1"><p className="text-sm text-text">{runtime ? `${runtime.provider === 'openai' ? 'OpenAI API' : 'Local Ollama'} · ${runtime.model}` : 'Loading runtime...'}</p><p className="mt-1 text-xs text-muted">{runtime?.provider === 'openai' ? (runtime.api_key_configured ? 'API key configured and encrypted on the server.' : 'An API key is required before OpenAI can run.') : runtime?.ollama?.available ? `${runtime.ollama.models.length} local model${runtime.ollama.models.length === 1 ? '' : 's'} detected.` : 'Local Ollama is unavailable.'}</p></div><Button variant="secondary" className="!py-1.5 text-xs" onClick={() => setModal('ai-runtime')}><Save size={13} /> Configure</Button></div>}
      </Section>

      <Section title="Session behavior" note="Same low-friction UX, still keeping the key out of persistent browser storage.">
        <div className="px-4 py-3 text-sm text-muted">
          The current browser session keeps the admin key in session storage only. Closing the session clears it.
        </div>
      </Section>

      <Section title="Security notes" note="What is protected now, and what still matters operationally.">
        <div className="px-4 py-3 text-sm text-muted">
          Admin keys are hashed with PBKDF2-SHA256 before being stored. Failed key checks are lock-limited to 5 tries with a 5-minute timeout per client.
        </div>
        <div className="border-t border-border px-4 py-3 text-sm text-muted">
          This makes brute force much harder, but the host machine still matters. Separating the agent machine from the services machine later is the right next step for stronger isolation.
        </div>
      </Section>

      <Section title="Danger Zone" note="Destructive actions require the current admin key and the exact confirmation phrase. A backup is created automatically before data changes.">
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-4"><div><p className="text-sm text-text">Reset all memory</p><p className="mt-1 text-xs text-muted">Remove every stored memory, evidence, entity, transcript, analysis, and vector record.</p></div><Button variant="danger" className="!py-1.5 text-xs" onClick={() => setModal('reset-all')}><Trash2 size={13} /> Reset all</Button></div>
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-4"><div><p className="text-sm text-text">Reset data from a date</p><p className="mt-1 text-xs text-muted">Remove only records created on or after a selected UTC moment.</p></div><Button variant="danger" className="!py-1.5 text-xs" onClick={() => setModal('reset-date')}><AlertTriangle size={13} /> Choose date</Button></div>
      </Section>

      {modal === 'change' && <ChangeKeyModal onClose={() => setModal(null)} onDone={showSuccess} />}
      {modal === 'rotate' && <RotateKeyModal onClose={() => setModal(null)} onDone={showSuccess} />}
      {modal === 'agent-key' && <AgentKeyModal onClose={() => setModal(null)} onCreated={() => loadStatus()} />}
      {modal === 'ai-runtime' && !piChooses && <AiRuntimeModal runtime={runtime} onClose={() => setModal(null)} onSaved={showSuccess} />}
      {modal === 'reset-all' && <ResetMemoryModal mode="all" onClose={() => setModal(null)} onReset={showSuccess} />}
      {modal === 'reset-date' && <ResetMemoryModal mode="date" onClose={() => setModal(null)} onReset={showSuccess} />}
    </div>
  );
}
