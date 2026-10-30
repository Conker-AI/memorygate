import { useEffect, useState } from 'react';
import { api } from './api';

// Stored data names stay as they are; only what the screen says changes.
const ANALYSIS_LABELS = { local_ai_proposal: 'AI proposal' };
const STAGE_LABELS = { local_ai: 'AI proposal' };

export const analysisLabel = (type) => ANALYSIS_LABELS[type] || type;
export const stageLabel = (stage) => STAGE_LABELS[stage] || stage;

// "qwen3:4b via ollama"; empty when nobody was named (the direct path, older records).
export function answeredBy(model, provider) {
  if (!model) return provider ? `a model via ${provider}` : '';
  return provider ? `${model} via ${provider}` : model;
}

// Who chooses MemoryGate's model: null until `/runtime/health` answers, then its model_choice
// (`managed_by` "pi" or "memorygate"). Screens keep the Ollama wording only for "memorygate".
// `reload` asks again, so "last answer" can follow a new answer.
export function useModelChoice() {
  const [choice, setChoice] = useState(null);
  const [asked, setAsked] = useState(0);
  useEffect(() => {
    let live = true;
    api.get('/runtime/health')
      .then((health) => { if (live) setChoice(health?.model_choice || null); })
      .catch(() => {});
    return () => { live = false; };
  }, [asked]);
  return [choice, () => setAsked((count) => count + 1)];
}
