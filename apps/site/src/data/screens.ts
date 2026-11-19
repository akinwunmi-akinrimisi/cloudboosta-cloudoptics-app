// Product screenshots taken from the demo build by scripts/screenshots.mjs (sample data).
// A missing file falls back to a labelled placeholder, so the site builds before SITE-06 lands.
import type { ImageMetadata } from 'astro'

const files = import.meta.glob<{ default: ImageMetadata }>('../assets/screens/*.png', { eager: true })

export function screen(name: string): ImageMetadata | null {
  return files[`../assets/screens/${name}.png`]?.default ?? null
}
