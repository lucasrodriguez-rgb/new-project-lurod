import { Search, RotateCcw, ArrowUpDown, Trash2 } from 'lucide-react';
import type { Filters, FilterStatus, Category, Priority, SortBy } from '../types/task';
import { CATEGORY_CONFIG, PRIORITY_CONFIG } from '../types/task';

interface FilterBarProps {
  filters: Filters;
  onUpdateFilter: <K extends keyof Filters>(key: K, value: Filters[K]) => void;
  onResetFilters: () => void;
  onClearCompleted: () => void;
  completedCount: number;
}

export function FilterBar({
  filters,
  onUpdateFilter,
  onResetFilters,
  onClearCompleted,
  completedCount,
}: FilterBarProps) {
  const statusOptions: { value: FilterStatus; label: string }[] = [
    { value: 'all', label: 'All' },
    { value: 'active', label: 'Active' },
    { value: 'completed', label: 'Done' },
  ];

  const sortOptions: { value: SortBy; label: string }[] = [
    { value: 'createdAt', label: 'Date Created' },
    { value: 'priority', label: 'Priority' },
    { value: 'dueDate', label: 'Due Date' },
    { value: 'title', label: 'Title' },
  ];

  const isFiltered =
    filters.status !== 'all' ||
    filters.category !== 'all' ||
    filters.priority !== 'all' ||
    filters.search !== '';

  return (
    <div className="space-y-3">
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
        <input
          type="text"
          placeholder="Search tasks..."
          value={filters.search}
          onChange={(e) => onUpdateFilter('search', e.target.value)}
          className="w-full bg-white/5 border border-white/10 rounded-xl pl-10 pr-4 py-2.5 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500/50 focus:border-primary-500/50 transition-all"
        />
      </div>

      <div className="flex flex-wrap gap-2 items-center">
        <div className="flex bg-white/5 rounded-lg p-0.5 border border-white/10">
          {statusOptions.map((opt) => (
            <button
              key={opt.value}
              onClick={() => onUpdateFilter('status', opt.value)}
              className={`px-3 py-1.5 rounded-md text-xs font-medium transition-all cursor-pointer ${
                filters.status === opt.value
                  ? 'bg-primary-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-white'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>

        <select
          value={filters.category}
          onChange={(e) => onUpdateFilter('category', e.target.value as Category | 'all')}
          className="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-xs text-slate-300 focus:outline-none focus:ring-2 focus:ring-primary-500/50 appearance-none cursor-pointer"
        >
          <option value="all" className="bg-slate-800">All Categories</option>
          {(Object.entries(CATEGORY_CONFIG) as [Category, typeof CATEGORY_CONFIG.personal][]).map(
            ([key, cfg]) => (
              <option key={key} value={key} className="bg-slate-800">
                {cfg.emoji} {cfg.label}
              </option>
            ),
          )}
        </select>

        <select
          value={filters.priority}
          onChange={(e) => onUpdateFilter('priority', e.target.value as Priority | 'all')}
          className="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-xs text-slate-300 focus:outline-none focus:ring-2 focus:ring-primary-500/50 appearance-none cursor-pointer"
        >
          <option value="all" className="bg-slate-800">All Priorities</option>
          {(Object.entries(PRIORITY_CONFIG) as [Priority, typeof PRIORITY_CONFIG.high][]).map(
            ([key, cfg]) => (
              <option key={key} value={key} className="bg-slate-800">
                {cfg.label}
              </option>
            ),
          )}
        </select>

        <div className="flex items-center gap-1 bg-white/5 border border-white/10 rounded-lg">
          <select
            value={filters.sortBy}
            onChange={(e) => onUpdateFilter('sortBy', e.target.value as SortBy)}
            className="bg-transparent px-3 py-1.5 text-xs text-slate-300 focus:outline-none appearance-none cursor-pointer"
          >
            {sortOptions.map((opt) => (
              <option key={opt.value} value={opt.value} className="bg-slate-800">
                {opt.label}
              </option>
            ))}
          </select>
          <button
            onClick={() =>
              onUpdateFilter('sortDirection', filters.sortDirection === 'asc' ? 'desc' : 'asc')
            }
            className="p-1.5 text-slate-400 hover:text-white transition-colors cursor-pointer"
            title={`Sort ${filters.sortDirection === 'asc' ? 'descending' : 'ascending'}`}
          >
            <ArrowUpDown className="w-3.5 h-3.5" />
          </button>
        </div>

        {isFiltered && (
          <button
            onClick={onResetFilters}
            className="flex items-center gap-1 text-xs text-slate-400 hover:text-white transition-colors cursor-pointer"
          >
            <RotateCcw className="w-3 h-3" />
            Reset
          </button>
        )}

        {completedCount > 0 && (
          <button
            onClick={onClearCompleted}
            className="ml-auto flex items-center gap-1 text-xs text-slate-400 hover:text-red-400 transition-colors cursor-pointer"
          >
            <Trash2 className="w-3 h-3" />
            Clear {completedCount} done
          </button>
        )}
      </div>
    </div>
  );
}
