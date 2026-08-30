import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  collectNonLocalReferences,
  processChartDirectory,
  vendorExternalDefinitionReferences,
} from "./vendor-helm-schema-refs.mjs";

const definitionsURL = "https://schemas.example.test/_definitions.json";

test("vendors remote top-level definitions and preserves local references", async () => {
  const schema = {
    type: "object",
    properties: {
      pod: { $ref: `${definitionsURL}#/definitions/Pod` },
      policy: { $ref: `${definitionsURL}#/definitions/PullPolicy` },
    },
  };
  const remoteSchema = {
    definitions: {
      Pod: {
        type: "object",
        properties: {
          policy: { $ref: "#/definitions/PullPolicy" },
        },
      },
      PullPolicy: { type: "string", enum: ["Always", "IfNotPresent"] },
    },
  };

  let loads = 0;
  const result = await vendorExternalDefinitionReferences(schema, async (url) => {
    assert.equal(url, definitionsURL);
    loads += 1;
    return { document: structuredClone(remoteSchema), sha256: "abc123" };
  });

  assert.equal(loads, 1);
  assert.equal(result.referencesRewritten, 2);
  assert.deepEqual(collectNonLocalReferences(schema), []);
  assert.equal(schema.properties.pod.$ref, "#/definitions/Pod");
  assert.equal(schema.definitions.Pod.properties.policy.$ref, "#/definitions/PullPolicy");
  assert.deepEqual(schema.definitions.PullPolicy.enum, ["Always", "IfNotPresent"]);
  assert.deepEqual(result.sourceDocuments, [
    { url: definitionsURL, sha256: "abc123", definitions: 2 },
  ]);
});

test("rejects conflicting definitions instead of silently changing validation", async () => {
  const schema = {
    definitions: { Shared: { type: "number" } },
    properties: {
      value: { $ref: `${definitionsURL}#/definitions/Shared` },
    },
  };

  await assert.rejects(
    vendorExternalDefinitionReferences(schema, async () => ({
      definitions: { Shared: { type: "string" } },
    })),
    /definition collision/,
  );
});

test("rejects insecure or unsupported remote reference shapes", async () => {
  await assert.rejects(
    vendorExternalDefinitionReferences(
      { properties: { value: { $ref: "http://schemas.example.test/defs.json#/definitions/X" } } },
      async () => ({ definitions: { X: { type: "string" } } }),
    ),
    /must use HTTPS/,
  );

  await assert.rejects(
    vendorExternalDefinitionReferences(
      { properties: { value: { $ref: `${definitionsURL}#named-anchor` } } },
      async () => ({ definitions: { X: { type: "string" } } }),
    ),
    /must target #\/definitions/,
  );
});

test("processes nested dependency schemas and check mode remains read-only", async () => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "helm-schema-vendor-"));
  const chartDirectory = path.join(temporaryDirectory, "chart");
  const nestedDirectory = path.join(chartDirectory, "charts", "dependency");
  const schemaPath = path.join(nestedDirectory, "values.schema.json");
  await mkdir(nestedDirectory, { recursive: true });
  await writeFile(
    schemaPath,
    `${JSON.stringify({ properties: { value: { $ref: `${definitionsURL}#/definitions/X` } } })}\n`,
  );

  try {
    const check = await processChartDirectory(chartDirectory, { checkOnly: true });
    assert.equal(check.schemaFiles, 1);
    assert.equal(check.filesChanged, 0);
    assert.equal(check.nonLocalReferences, 1);
    assert.match(await readFile(schemaPath, "utf8"), /^\{"properties"/);

    const vendored = await processChartDirectory(chartDirectory, {
      loadDocument: async () => ({
        document: { definitions: { X: { type: "string" } } },
        sha256: "def456",
      }),
    });
    assert.equal(vendored.filesChanged, 1);
    assert.equal(vendored.referencesRewritten, 1);

    const updated = JSON.parse(await readFile(schemaPath, "utf8"));
    assert.equal(updated.properties.value.$ref, "#/definitions/X");
    assert.deepEqual(updated.definitions.X, { type: "string" });
  } finally {
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
});
