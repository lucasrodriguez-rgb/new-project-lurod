import { Sparkles } from 'lucide-react';
import { useTaskStore } from './store/useTaskStore';
import { StatsBar } from './components/StatsBar';
import { TaskForm } from './components/TaskForm';
import { FilterBar } from './components/FilterBar';
import { TaskCard } from './components/TaskCard';
import { EmptyState } from './components/EmptyState';

function App() {
  const {
    tasks,
    filters,
    stats,
    addTask,
    toggleTask,
    deleteTask,
    editTask,
    clearCompleted,
    updateFilter,
    resetFilters,
  } = useTaskStore();

  const hasFilters =
    filters.status !== 'all' ||
    filters.category !== 'all' ||
    filters.priority !== 'all' ||
    filters.search !== '';

  return (
    <div className="min-h-screen">
      <div className="absolute inset-0 overflow-hidden pointer-events-none">
        <div className="absolute -top-40 -right-40 w-80 h-80 bg-primary-600/10 rounded-full blur-3xl" />
        <div className="absolute -bottom-40 -left-40 w-80 h-80 bg-purple-600/10 rounded-full blur-3xl" />
      </div>

      <div className="relative max-w-2xl mx-auto px-4 py-8 sm:py-12">
        <header className="text-center mb-8">
          <div className="inline-flex items-center gap-2 mb-3">
            <div className="w-10 h-10 bg-gradient-to-br from-primary-500 to-purple-500 rounded-xl flex items-center justify-center shadow-lg shadow-primary-500/25">
              <Sparkles className="w-5 h-5 text-white" />
            </div>
            <h1 className="text-3xl font-bold bg-gradient-to-r from-white to-slate-300 bg-clip-text text-transparent">
              Taskflow
            </h1>
          </div>
          <p className="text-slate-400 text-sm">
            Organize your day, one task at a time
          </p>
        </header>

        <div className="space-y-6">
          <StatsBar stats={stats} />
          <TaskForm onAdd={addTask} />

          {stats.total > 0 && (
            <FilterBar
              filters={filters}
              onUpdateFilter={updateFilter}
              onResetFilters={resetFilters}
              onClearCompleted={clearCompleted}
              completedCount={stats.completed}
            />
          )}

          {tasks.length > 0 ? (
            <div className="space-y-2">
              {tasks.map((task) => (
                <TaskCard
                  key={task.id}
                  task={task}
                  onToggle={toggleTask}
                  onDelete={deleteTask}
                  onEdit={editTask}
                />
              ))}
              <p className="text-center text-xs text-slate-600 pt-2">
                Showing {tasks.length} of {stats.total} tasks
              </p>
            </div>
          ) : (
            <EmptyState hasFilters={hasFilters} />
          )}
        </div>

        <footer className="text-center mt-12 text-xs text-slate-600">
          Built with React, TypeScript & Tailwind CSS
        </footer>
      </div>
    </div>
  );
}

export default App;
