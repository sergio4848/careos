"use client";

import { useSyncExternalStore } from "react";

import { serverNow } from "./server-clock";

/** One shared 1-second ticker for every timer on screen (no interval per component). */
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;
let current = serverNow();
/** The browser's own clock, for comparing against browser timestamps (e.g. react-query's). */
let currentLocal = Date.now();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    current = serverNow();
    currentLocal = Date.now();
    timer = setInterval(() => {
      current = serverNow();
      currentLocal = Date.now();
      listeners.forEach((notify) => notify());
    }, 1000);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

const getSnapshot = () => current;
const getServerSnapshot = () => 0;

export function useNow(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}

const getLocalSnapshot = () => currentLocal;

/** Ticks with `useNow`, but on the local clock (use with timestamps taken in this browser). */
export function useLocalNow(): number {
  return useSyncExternalStore(subscribe, getLocalSnapshot, getServerSnapshot);
}
