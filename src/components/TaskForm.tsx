import { useState } from 'react';
import { Plus, X } from 'lucide-react';
import type { Priority, Category } from '../types/task';
import { CATEGORY_CONFIG, PRIORITY_CONFIG } from '../types/task';

interface TaskFormProps {
  onAdd: (input: {
    title: string;
    description: string;
    priority: Priority;
    category: Category;
    dueDate: string | null;
  }) => void;
}

export function TaskForm({ onAdd }: TaskFormProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [priority, setPriority] = useState<Priority>('medium');
  const [category, setCategory] = useState<Category>('personal');
  const [dueDate, setDueDate] = useState('');

  const reset = () => {
    setTitle('');
    setDescription('');
    setPriority('medium');
    setCategory('personal');
    setDueDate('');
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;
    onAdd({ title, description, priority, category, dueDate: dueDate || null });
    reset();
    setIsOpen(false);
  };

  if (!isOpen) {
    return (
      <button
        onClick={() => setIsOpen(true)}
        className="w-full bg-primary-600 hover:bg-primary-500 text-white font-medium py-3 px-6 rounded-xl transition-all duration-200 flex items-center justify-center gap-2 shadow-lg shadow-primary-600/20 cursor-pointer"
      >
        <Plus className="w-5 h-5" />
        Add New Task
      </button>
    );
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-white/5 backdrop-blur-sm border border-white/10 rounded-xl p-5 space-y-4 animate-in"
    >
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-semibold text-white">New Task</h3>
        <button
          type="button"
          onClick={() => { setIsOpen(false); reset(); }}
          className="text-slate-400 hover:text-white transition-colors cursor-pointer"
        >
          <X className="w-5 h-5" />
        </button>
      </div>

      <div>
        <input
          type="text"
          placeholder="What needs to be done?"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="w-full bg-white/5 border border-white/10 rounded-lg px-4 py-2.5 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500/50 focus:border-primary-500/50 transition-all"
          autoFocus
        />
      </div>

      <div>
        <textarea
          placeholder="Add a description (optional)"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
          className="w-full bg-white/5 border border-white/10 rounded-lg px-4 py-2.5 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500/50 focus:border-primary-500/50 transition-all resize-none"
        />
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div>
          <label className="block text-xs text-slate-400 mb-1.5">Priority</label>
          <div className="flex gap-1.5">
            {(Object.entries(PRIORITY_CONFIG) as [Priority, typeof PRIORITY_CONFIG.high][]).map(
              ([key, cfg]) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setPriority(key)}
                  className={`flex-1 py-1.5 px-2 rounded-lg text-xs font-medium transition-all cursor-pointer border ${
                    priority === key
                      ? 'border-white/20 text-white'
                      : 'border-transparent text-slate-400 hover:text-white'
                  }`}
                  style={{
                    backgroundColor: priority === key ? cfg.color + '30' : 'transparent',
                  }}
                >
                  {cfg.label}
                </button>
              ),
            )}
          </div>
        </div>

        <div>
          <label className="block text-xs text-slate-400 mb-1.5">Category</label>
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value as Category)}
            className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white focus:outline-none focus:ring-2 focus:ring-primary-500/50 appearance-none cursor-pointer"
          >
            {(Object.entries(CATEGORY_CONFIG) as [Category, typeof CATEGORY_CONFIG.personal][]).map(
              ([key, cfg]) => (
                <option key={key} value={key} className="bg-slate-800">
                  {cfg.emoji} {cfg.label}
                </option>
              ),
            )}
          </select>
        </div>

        <div>
          <label className="block text-xs text-slate-400 mb-1.5">Due Date</label>
          <input
            type="date"
            value={dueDate}
            onChange={(e) => setDueDate(e.target.value)}
            className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white focus:outline-none focus:ring-2 focus:ring-primary-500/50 cursor-pointer"
          />
        </div>
      </div>

      <div className="flex gap-2 pt-1">
        <button
          type="submit"
          disabled={!title.trim()}
          className="flex-1 bg-primary-600 hover:bg-primary-500 disabled:opacity-40 disabled:cursor-not-allowed text-white font-medium py-2.5 px-4 rounded-lg transition-all cursor-pointer"
        >
          Add Task
        </button>
        <button
          type="button"
          onClick={() => { setIsOpen(false); reset(); }}
          className="px-4 py-2.5 text-slate-400 hover:text-white border border-white/10 rounded-lg transition-all cursor-pointer"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
