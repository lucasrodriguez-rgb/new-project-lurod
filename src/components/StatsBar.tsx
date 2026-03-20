import { CheckCircle2, Circle, AlertTriangle, LayoutList } from 'lucide-react';

interface StatsBarProps {
  stats: { total: number; completed: number; active: number; overdue: number };
}

export function StatsBar({ stats }: StatsBarProps) {
  const items = [
    { label: 'Total', value: stats.total, icon: LayoutList, color: 'text-primary-400' },
    { label: 'Active', value: stats.active, icon: Circle, color: 'text-sky-400' },
    { label: 'Completed', value: stats.completed, icon: CheckCircle2, color: 'text-emerald-400' },
    { label: 'Overdue', value: stats.overdue, icon: AlertTriangle, color: 'text-red-400' },
  ];

  const pct = stats.total > 0 ? Math.round((stats.completed / stats.total) * 100) : 0;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {items.map((item) => (
          <div
            key={item.label}
            className="bg-white/5 backdrop-blur-sm border border-white/10 rounded-xl p-4 flex items-center gap-3"
          >
            <item.icon className={`w-5 h-5 ${item.color} shrink-0`} />
            <div>
              <p className="text-2xl font-bold text-white">{item.value}</p>
              <p className="text-xs text-slate-400">{item.label}</p>
            </div>
          </div>
        ))}
      </div>

      {stats.total > 0 && (
        <div className="bg-white/5 backdrop-blur-sm border border-white/10 rounded-xl p-4">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm text-slate-400">Progress</span>
            <span className="text-sm font-medium text-white">{pct}%</span>
          </div>
          <div className="w-full bg-slate-700/50 rounded-full h-2.5 overflow-hidden">
            <div
              className="bg-gradient-to-r from-primary-500 to-emerald-400 h-full rounded-full transition-all duration-500 ease-out"
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
      )}
    </div>
  );
}
