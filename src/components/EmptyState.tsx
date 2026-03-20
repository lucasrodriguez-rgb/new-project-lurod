import { ClipboardList, Search } from 'lucide-react';

interface EmptyStateProps {
  hasFilters: boolean;
}

export function EmptyState({ hasFilters }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      {hasFilters ? (
        <>
          <div className="w-14 h-14 rounded-2xl bg-white/5 border border-white/10 flex items-center justify-center mb-4">
            <Search className="w-7 h-7 text-slate-500" />
          </div>
          <h3 className="text-lg font-medium text-slate-300 mb-1">No matching tasks</h3>
          <p className="text-sm text-slate-500 max-w-xs">
            Try adjusting your filters or search query to find what you're looking for.
          </p>
        </>
      ) : (
        <>
          <div className="w-14 h-14 rounded-2xl bg-white/5 border border-white/10 flex items-center justify-center mb-4">
            <ClipboardList className="w-7 h-7 text-slate-500" />
          </div>
          <h3 className="text-lg font-medium text-slate-300 mb-1">No tasks yet</h3>
          <p className="text-sm text-slate-500 max-w-xs">
            Click the button above to add your first task and start getting organized.
          </p>
        </>
      )}
    </div>
  );
}
