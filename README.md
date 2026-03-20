# Taskflow

A modern, beautiful task management application built with React, TypeScript, and Tailwind CSS.

## Features

- **Create tasks** with title, description, priority, category, and due date
- **Edit and delete** tasks inline with confirmation
- **Mark tasks complete** with a satisfying visual toggle
- **Filter and search** by status, category, priority, or free text
- **Sort** by date created, priority, due date, or title
- **Progress tracking** with stats dashboard and progress bar
- **Overdue detection** highlights tasks past their due date
- **Persistent storage** using localStorage — your tasks survive page reloads
- **Dark mode UI** with a polished, modern glassmorphism design
- **Fully responsive** layout for mobile and desktop

## Tech Stack

- [React](https://react.dev/) 19 with hooks
- [TypeScript](https://www.typescriptlang.org/) for type safety
- [Vite](https://vite.dev/) for fast development and builds
- [Tailwind CSS](https://tailwindcss.com/) v4 for utility-first styling
- [Lucide React](https://lucide.dev/) for icons

## Getting Started

```bash
# Install dependencies
npm install

# Start the development server
npm run dev

# Build for production
npm run build

# Preview the production build
npm run preview
```

## Project Structure

```
src/
  types/task.ts          # Type definitions, category & priority config
  store/useTaskStore.ts  # Task state management with localStorage
  components/
    StatsBar.tsx         # Stats dashboard with progress bar
    TaskForm.tsx         # New task creation form
    TaskCard.tsx         # Individual task display with edit/delete
    FilterBar.tsx        # Search, filter, and sort controls
    EmptyState.tsx       # Empty state illustrations
  App.tsx                # Main application layout
  main.tsx               # Entry point
  index.css              # Tailwind imports and global styles
```
