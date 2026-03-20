export type Priority = 'low' | 'medium' | 'high';

export type Category = 'personal' | 'work' | 'health' | 'learning' | 'errands' | 'other';

export interface Task {
  id: string;
  title: string;
  description: string;
  priority: Priority;
  category: Category;
  completed: boolean;
  createdAt: string;
  completedAt: string | null;
  dueDate: string | null;
}

export type SortBy = 'createdAt' | 'priority' | 'dueDate' | 'title';
export type FilterStatus = 'all' | 'active' | 'completed';

export interface Filters {
  status: FilterStatus;
  category: Category | 'all';
  priority: Priority | 'all';
  search: string;
  sortBy: SortBy;
  sortDirection: 'asc' | 'desc';
}

export const CATEGORY_CONFIG: Record<Category, { label: string; color: string; emoji: string }> = {
  personal: { label: 'Personal', color: '#a78bfa', emoji: '🏠' },
  work: { label: 'Work', color: '#60a5fa', emoji: '💼' },
  health: { label: 'Health', color: '#34d399', emoji: '💪' },
  learning: { label: 'Learning', color: '#fbbf24', emoji: '📚' },
  errands: { label: 'Errands', color: '#f472b6', emoji: '🛒' },
  other: { label: 'Other', color: '#94a3b8', emoji: '📌' },
};

export const PRIORITY_CONFIG: Record<Priority, { label: string; color: string; order: number }> = {
  high: { label: 'High', color: '#ef4444', order: 3 },
  medium: { label: 'Medium', color: '#f59e0b', order: 2 },
  low: { label: 'Low', color: '#22c55e', order: 1 },
};
