# ADR-005: React SPA: Vite, TanStack, Tailwind, Radix, ECharts

**Status:** Proposed

## Context

The app is authenticated and data-dense. It must load on 3G-class networks: the initial JS budget is 300 KB gzipped (PRD FR-UI-10).

## Decision

React 19 + TypeScript strict, Vite, TanStack Router (filters in the URL), TanStack Query, Zustand, Tailwind 4 with Cloudoptic design tokens, Radix primitives (shadcn pattern), TanStack Table, Apache ECharts, openapi-fetch client. Vitest + Testing Library; oxlint for linting (Vite's current default and much faster than ESLint). CI enforces the bundle budget.

## Consequences

No AG Grid licence; grouping happens on the server (a pattern confirmed by the Flexera teardown, docs/12 §6.5).

## Alternatives considered

Next.js (no SSR need); MUI or Ant Design (heavy, hard to brand); AG Grid (licence, bundle size).

## References

docs/11a §3, docs/12 §6.5
