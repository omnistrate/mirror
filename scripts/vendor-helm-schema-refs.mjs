#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readdir, readFile, writeFile } from "node:fs/promises";
import { isDeepStrictEqual } from "node:util";
import path from "node:path";
import { pathToFileURL } from "node:url";

const MAX_REMOTE_SCHEMA_BYTES = 10 * 1024 * 1024;

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function visitSchemaValues(value, visitor) {
  if (Array.isArray(value)) {
    for (const item of value) {
      visitSchemaValues(item, visitor);
    }
    return;
  }

  if (!isObject(value)) {
    return;
  }

  visitor(value);
  for (const child of Object.values(value)) {
    visitSchemaValues(child, visitor);
  }
}

export function collectNonLocalReferences(schema) {
  const references = [];

  visitSchemaValues(schema, (value) => {
    if (typeof value.$ref === "string" && !value.$ref.startsWith("#")) {
      references.push(value.$ref);
    }
  });

  return references;
}

function parseRemoteDefinitionReference(reference) {
  let url;
  try {
    url = new URL(reference);
  } catch {
    throw new Error(`unsupported non-local JSON Schema reference: ${reference}`);
  }

  if (url.protocol !== "https:") {
    throw new Error(`remote JSON Schema references must use HTTPS: ${reference}`);
  }

  if (!url.hash.startsWith("#/definitions/")) {
    throw new Error(
      `remote JSON Schema reference must target #/definitions/...: ${reference}`,
    );
  }

  const fragment = url.hash;
  url.hash = "";
  return { documentURL: url.toString(), fragment };
}

function rewriteDocumentReferences(schema, documentURL) {
  let rewritten = 0;

  visitSchemaValues(schema, (value) => {
    if (typeof value.$ref !== "string" || value.$ref.startsWith("#")) {
      return;
    }

    const parsed = parseRemoteDefinitionReference(value.$ref);
    if (parsed.documentURL === documentURL) {
      value.$ref = parsed.fragment;
      rewritten += 1;
    }
  });

  return rewritten;
}

function mergeDefinitions(schema, remoteSchema, documentURL) {
  if (!isObject(remoteSchema.definitions)) {
    throw new Error(`remote JSON Schema has no top-level definitions object: ${documentURL}`);
  }

  if (schema.definitions === undefined) {
    schema.definitions = {};
  } else if (!isObject(schema.definitions)) {
    throw new Error("values.schema.json definitions must be an object");
  }

  for (const [name, definition] of Object.entries(remoteSchema.definitions)) {
    if (!(name in schema.definitions)) {
      schema.definitions[name] = definition;
      continue;
    }

    if (!isDeepStrictEqual(schema.definitions[name], definition)) {
      throw new Error(
        `definition collision while vendoring ${documentURL}: ${name}`,
      );
    }
  }
}

export async function vendorExternalDefinitionReferences(schema, loadDocument) {
  const sourceDocuments = new Map();
  let referencesRewritten = 0;

  while (true) {
    const references = collectNonLocalReferences(schema);
    if (references.length === 0) {
      break;
    }

    const documents = new Map();
    for (const reference of references) {
      const parsed = parseRemoteDefinitionReference(reference);
      documents.set(parsed.documentURL, parsed);
    }

    let rewritesThisPass = 0;
    for (const documentURL of documents.keys()) {
      if (!sourceDocuments.has(documentURL)) {
        const loaded = await loadDocument(documentURL);
        const remoteSchema = loaded.document ?? loaded;
        mergeDefinitions(schema, remoteSchema, documentURL);
        sourceDocuments.set(documentURL, {
          url: documentURL,
          sha256:
            loaded.sha256 ??
            createHash("sha256").update(JSON.stringify(remoteSchema)).digest("hex"),
          definitions: Object.keys(remoteSchema.definitions).length,
        });
      }

      rewritesThisPass += rewriteDocumentReferences(schema, documentURL);
    }

    if (rewritesThisPass === 0) {
      throw new Error("unable to rewrite non-local JSON Schema references");
    }
    referencesRewritten += rewritesThisPass;
  }

  return {
    referencesRewritten,
    sourceDocuments: [...sourceDocuments.values()].sort((left, right) =>
      left.url.localeCompare(right.url),
    ),
  };
}

async function findValuesSchemaFiles(rootDirectory) {
  const files = [];
  const entries = await readdir(rootDirectory, { withFileTypes: true });

  for (const entry of entries) {
    const entryPath = path.join(rootDirectory, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await findValuesSchemaFiles(entryPath)));
    } else if (entry.isFile() && entry.name === "values.schema.json") {
      files.push(entryPath);
    }
  }

  return files.sort();
}

async function fetchRemoteSchema(documentURL) {
  const response = await fetch(documentURL, {
    headers: { accept: "application/schema+json, application/json" },
    redirect: "follow",
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) {
    throw new Error(`failed to fetch ${documentURL}: HTTP ${response.status}`);
  }
  if (new URL(response.url).protocol !== "https:") {
    throw new Error(`remote JSON Schema redirected away from HTTPS: ${documentURL}`);
  }

  const content = Buffer.from(await response.arrayBuffer());
  if (content.length > MAX_REMOTE_SCHEMA_BYTES) {
    throw new Error(
      `remote JSON Schema exceeds ${MAX_REMOTE_SCHEMA_BYTES} bytes: ${documentURL}`,
    );
  }

  let document;
  try {
    document = JSON.parse(content.toString("utf8"));
  } catch (error) {
    throw new Error(`remote JSON Schema is not valid JSON: ${documentURL}`, {
      cause: error,
    });
  }

  return {
    document,
    sha256: createHash("sha256").update(content).digest("hex"),
  };
}

export async function processChartDirectory(
  chartDirectory,
  { checkOnly = false, loadDocument = fetchRemoteSchema } = {},
) {
  const schemaFiles = await findValuesSchemaFiles(chartDirectory);
  const documentCache = new Map();
  const sourceDocuments = new Map();
  const filesWithNonLocalReferences = [];
  let nonLocalReferences = 0;
  let filesChanged = 0;
  let referencesRewritten = 0;

  const cachedLoader = async (documentURL) => {
    if (!documentCache.has(documentURL)) {
      documentCache.set(documentURL, loadDocument(documentURL));
    }
    return documentCache.get(documentURL);
  };

  for (const schemaFile of schemaFiles) {
    const original = await readFile(schemaFile, "utf8");
    const schema = JSON.parse(original);
    const references = collectNonLocalReferences(schema);
    if (references.length === 0) {
      continue;
    }

    nonLocalReferences += references.length;
    filesWithNonLocalReferences.push(path.relative(chartDirectory, schemaFile));
    if (checkOnly) {
      continue;
    }

    const result = await vendorExternalDefinitionReferences(schema, cachedLoader);
    referencesRewritten += result.referencesRewritten;
    for (const source of result.sourceDocuments) {
      sourceDocuments.set(source.url, source);
    }

    await writeFile(schemaFile, `${JSON.stringify(schema, null, 2)}\n`);
    filesChanged += 1;
  }

  return {
    schemaFiles: schemaFiles.length,
    filesChanged,
    nonLocalReferences,
    referencesRewritten,
    filesWithNonLocalReferences,
    sourceDocuments: [...sourceDocuments.values()].sort((left, right) =>
      left.url.localeCompare(right.url),
    ),
  };
}

async function main() {
  const args = process.argv.slice(2);
  const checkOnly = args[0] === "--check";
  const chartDirectory = checkOnly ? args[1] : args[0];
  if (!chartDirectory || args.length !== (checkOnly ? 2 : 1)) {
    throw new Error(
      "usage: vendor-helm-schema-refs.mjs [--check] <unpacked-chart-directory>",
    );
  }

  const summary = await processChartDirectory(path.resolve(chartDirectory), {
    checkOnly,
  });
  process.stdout.write(`${JSON.stringify(summary)}\n`);
  if (checkOnly && summary.nonLocalReferences > 0) {
    process.exitCode = 2;
  }
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.stack ?? error.message);
    process.exitCode = 1;
  });
}
