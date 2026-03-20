import { useState, useCallback, useMemo } from 'react';
import type { Task, Filters, Priority, Category, SortBy } from '../types/task';
import { PRIORITY_CONFIG } from '../types/task';

const STORAGE_KEY = 'taskflow-tasks';

function generateId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 9);
}

function loadTasks(): Task[] {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored ? JSON.parse(stored) : [];
  } catch {
    return [];
  }
}

function saveTasks(tasks: Task[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(tasks));
}

const DEFAULT_FILTERS: Filters = {
  status: 'all',
  category: 'all',
  priority: 'all',
  search: '',
  sortBy: 'createdAt',
  sortDirection: 'desc',
};

export function useTaskStore() {
  const [tasks, setTasks] = useState<Task[]>(loadTasks);
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);

  const updateTasks = useCallback((updater: (prev: Task[]) => Task[]) => {
    setTasks((prev) => {
      const next = updater(prev);
      saveTasks(next);
      return next;
    });
  }, []);

  const addTask = useCallback(
    (input: { title: string; description: string; priority: Priority; category: Category; dueDate: string | null }) => {
      const task: Task = {
        id: generateId(),
        title: input.title.trim(),
        description: input.description.trim(),
        priority: input.priority,
        category: input.category,
        completed: false,
        createdAt: new Date().toISOString(),
        completedAt: null,
        dueDate: input.dueDate,
      };
      updateTasks((prev) => [task, ...prev]);
    },
    [updateTasks],
  );

  const toggleTask = useCallback(
    (id: string) => {
      updateTasks((prev) =>
        prev.map((t) =>
          t.id === id
            ? { ...t, completed: !t.completed, completedAt: !t.completed ? new Date().toISOString() : null }
            : t,
        ),
      );
    },
    [updateTasks],
  );

  const deleteTask = useCallback(
    (id: string) => {
      updateTasks((prev) => prev.filter((t) => t.id !== id));
    },
    [updateTasks],
  );

  const editTask = useCallback(
    (id: string, updates: Partial<Omit<Task, 'id' | 'createdAt'>>) => {
      updateTasks((prev) => prev.map((t) => (t.id === id ? { ...t, ...updates } : t)));
    },
    [updateTasks],
  );

  const clearCompleted = useCallback(() => {
    updateTasks((prev) => prev.filter((t) => !t.completed));
  }, [updateTasks]);

  const updateFilter = useCallback(<K extends keyof Filters>(key: K, value: Filters[K]) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
  }, []);

  const resetFilters = useCallback(() => {
    setFilters(DEFAULT_FILTERS);
  }, []);

  const filteredTasks = useMemo(() => {
    let result = [...tasks];

    if (filters.status === 'active') result = result.filter((t) => !t.completed);
    else if (filters.status === 'completed') result = result.filter((t) => t.completed);

    if (filters.category !== 'all') result = result.filter((t) => t.category === filters.category);
    if (filters.priority !== 'all') result = result.filter((t) => t.priority === filters.priority);

    if (filters.search.trim()) {
      const q = filters.search.toLowerCase();
      result = result.filter(
        (t) => t.title.toLowerCase().includes(q) || t.description.toLowerCase().includes(q),
      );
    }

    const sortFns: Record<SortBy, (a: Task, b: Task) => number> = {
      createdAt: (a, b) => new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime(),
      priority: (a, b) => PRIORITY_CONFIG[a.priority].order - PRIORITY_CONFIG[b.priority].order,
      dueDate: (a, b) => {
        if (!a.dueDate && !b.dueDate) return 0;
        if (!a.dueDate) return 1;
        if (!b.dueDate) return -1;
        return new Date(a.dueDate).getTime() - new Date(b.dueDate).getTime();
      },
      title: (a, b) => a.title.localeCompare(b.title),
    };

    result.sort((a, b) => {
      const cmp = sortFns[filters.sortBy](a, b);
      return filters.sortDirection === 'desc' ? -cmp : cmp;
    });

    return result;
  }, [tasks, filters]);

  const stats = useMemo(() => {
    const total = tasks.length;
    const completed = tasks.filter((t) => t.completed).length;
    const active = total - completed;
    const overdue = tasks.filter(
      (t) => !t.completed && t.dueDate && new Date(t.dueDate) < new Date(),
    ).length;
    return { total, completed, active, overdue };
  }, [tasks]);

  return {
    tasks: filteredTasks,
    allTasks: tasks,
    filters,
    stats,
    addTask,
    toggleTask,
    deleteTask,
    editTask,
    clearCompleted,
    updateFilter,
    resetFilters,
  };
}
