import { useState } from 'react';
import { Check, Trash2, Calendar, Clock, Pencil, X, Save } from 'lucide-react';
import type { Task } from '../types/task';
import { CATEGORY_CONFIG, PRIORITY_CONFIG } from '../types/task';

interface TaskCardProps {
  task: Task;
  onToggle: (id: string) => void;
  onDelete: (id: string) => void;
  onEdit: (id: string, updates: Partial<Omit<Task, 'id' | 'createdAt'>>) => void;
}

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(dateStr).toLocaleDateString();
}

function formatDueDate(dateStr: string): { text: string; overdue: boolean } {
  const due = new Date(dateStr);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  due.setHours(0, 0, 0, 0);
  const diff = due.getTime() - today.getTime();
  const days = Math.round(diff / 86400000);

  if (days < 0) return { text: `${Math.abs(days)}d overdue`, overdue: true };
  if (days === 0) return { text: 'Due today', overdue: false };
  if (days === 1) return { text: 'Due tomorrow', overdue: false };
  return { text: `Due in ${days}d`, overdue: false };
}

export function TaskCard({ task, onToggle, onDelete, onEdit }: TaskCardProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [editTitle, setEditTitle] = useState(task.title);
  const [editDescription, setEditDescription] = useState(task.description);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const catCfg = CATEGORY_CONFIG[task.category];
  const priCfg = PRIORITY_CONFIG[task.priority];
  const dueInfo = task.dueDate ? formatDueDate(task.dueDate) : null;

  const handleSaveEdit = () => {
    if (!editTitle.trim()) return;
    onEdit(task.id, { title: editTitle.trim(), description: editDescription.trim() });
    setIsEditing(false);
  };

  const handleDelete = () => {
    if (confirmDelete) {
      onDelete(task.id);
    } else {
      setConfirmDelete(true);
      setTimeout(() => setConfirmDelete(false), 3000);
    }
  };

  return (
    <div
      className={`group bg-white/5 backdrop-blur-sm border rounded-xl p-4 transition-all duration-200 hover:bg-white/[0.07] ${
        task.completed ? 'border-white/5 opacity-60' : 'border-white/10'
      }`}
    >
      <div className="flex items-start gap-3">
        <button
          onClick={() => onToggle(task.id)}
          className={`mt-0.5 w-5 h-5 rounded-full border-2 flex items-center justify-center shrink-0 transition-all cursor-pointer ${
            task.completed
              ? 'bg-emerald-500 border-emerald-500'
              : 'border-slate-500 hover:border-primary-400'
          }`}
        >
          {task.completed && <Check className="w-3 h-3 text-white" />}
        </button>

        <div className="flex-1 min-w-0">
          {isEditing ? (
            <div className="space-y-2">
              <input
                type="text"
                value={editTitle}
                onChange={(e) => setEditTitle(e.target.value)}
                className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-white focus:outline-none focus:ring-2 focus:ring-primary-500/50"
                autoFocus
              />
              <textarea
                value={editDescription}
                onChange={(e) => setEditDescription(e.target.value)}
                rows={2}
                className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-white text-sm focus:outline-none focus:ring-2 focus:ring-primary-500/50 resize-none"
                placeholder="Description..."
              />
              <div className="flex gap-2">
                <button
                  onClick={handleSaveEdit}
                  className="flex items-center gap-1 text-xs text-emerald-400 hover:text-emerald-300 cursor-pointer"
                >
                  <Save className="w-3.5 h-3.5" /> Save
                </button>
                <button
                  onClick={() => {
                    setIsEditing(false);
                    setEditTitle(task.title);
                    setEditDescription(task.description);
                  }}
                  className="flex items-center gap-1 text-xs text-slate-400 hover:text-white cursor-pointer"
                >
                  <X className="w-3.5 h-3.5" /> Cancel
                </button>
              </div>
            </div>
          ) : (
            <>
              <p
                className={`font-medium leading-snug ${
                  task.completed ? 'line-through text-slate-500' : 'text-white'
                }`}
              >
                {task.title}
              </p>
              {task.description && (
                <p className="text-sm text-slate-400 mt-1 line-clamp-2">{task.description}</p>
              )}

              <div className="flex flex-wrap items-center gap-2 mt-2.5">
                <span
                  className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full"
                  style={{ backgroundColor: catCfg.color + '20', color: catCfg.color }}
                >
                  {catCfg.emoji} {catCfg.label}
                </span>

                <span
                  className="inline-flex items-center text-xs px-2 py-0.5 rounded-full font-medium"
                  style={{ backgroundColor: priCfg.color + '20', color: priCfg.color }}
                >
                  {priCfg.label}
                </span>

                {dueInfo && (
                  <span
                    className={`inline-flex items-center gap-1 text-xs ${
                      dueInfo.overdue && !task.completed ? 'text-red-400' : 'text-slate-400'
                    }`}
                  >
                    <Calendar className="w-3 h-3" />
                    {dueInfo.text}
                  </span>
                )}

                <span className="inline-flex items-center gap-1 text-xs text-slate-500">
                  <Clock className="w-3 h-3" />
                  {timeAgo(task.createdAt)}
                </span>
              </div>
            </>
          )}
        </div>

        {!isEditing && (
          <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
            {!task.completed && (
              <button
                onClick={() => setIsEditing(true)}
                className="p-1.5 text-slate-400 hover:text-white rounded-lg hover:bg-white/10 transition-all cursor-pointer"
                title="Edit task"
              >
                <Pencil className="w-3.5 h-3.5" />
              </button>
            )}
            <button
              onClick={handleDelete}
              className={`p-1.5 rounded-lg transition-all cursor-pointer ${
                confirmDelete
                  ? 'text-red-400 bg-red-400/10'
                  : 'text-slate-400 hover:text-red-400 hover:bg-white/10'
              }`}
              title={confirmDelete ? 'Click again to confirm' : 'Delete task'}
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
