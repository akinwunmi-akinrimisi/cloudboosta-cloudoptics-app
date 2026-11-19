// Astro integration: removes HTML comments from every built page (docs/14 §7.3, doc 13 §5.1:
// developer notes do not belong in production HTML). check-dist.mjs fails if any remain.
import { readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

export function htmlFiles(dir) {
  return readdirSync(dir, { withFileTypes: true, recursive: true })
    .filter((e) => e.isFile() && e.name.endsWith('.html'))
    .map((e) => join(e.parentPath, e.name))
}

export const COMMENT = /<!--[\s\S]*?-->/g

export default function stripHtmlComments() {
  return {
    name: 'cloudoptics:strip-html-comments',
    hooks: {
      'astro:build:done': ({ dir, logger }) => {
        let count = 0
        for (const file of htmlFiles(fileURLToPath(dir))) {
          const html = readFileSync(file, 'utf8')
          const out = html.replace(COMMENT, () => (count++, ''))
          if (out !== html) writeFileSync(file, out)
        }
        logger.info(`removed ${count} HTML comment(s)`)
      },
    },
  }
}
