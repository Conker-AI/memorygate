import { BrainCircuit } from 'lucide-react';
import { answeredBy } from '../lib/modelChoice';
import { timeAgo } from '../lib/timeAgo';

// Shown only when Conker (Pi) chooses the model: where to change it, and who answered last.
export default function ModelChoiceNote({ choice, className = '' }) {
  if (choice?.managed_by !== 'pi') return null;
  const last = choice.last_answer;
  const by = last ? answeredBy(last.model, last.provider) : '';
  return <p className={`flex items-start gap-2 text-xs text-muted ${className}`}>
    <BrainCircuit size={14} className="mt-px shrink-0 text-accent" />
    <span>The model is chosen in Conker: Settings, Models, Memory sorting.{by && ` Last answer by ${by}${last.at ? `, ${timeAgo(last.at)}` : ''}.`}</span>
  </p>;
}
