"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { CddCaseView } from "./CddCaseView";
import { CapabilityPanel } from "./CapabilityPanel";
import { DocumentPanel } from "./DocumentPanel";
import { PerpetualKycPanel } from "./PerpetualKycPanel";
import { UboGraphPanel } from "./UboGraphPanel";
import { DocumentViewerProvider } from "./DocumentViewerModal";
import { Panel } from "./ui";
import {
  AuthRequiredError,
  assessCdd,
  capabilities,
  configureApiTransport,
  exportPortableDossier,
  health,
  importPortableDossier,
  isBlocked,
  listDocuments,
  listPersonas,
  setDevPersona,
} from "../lib/api";
import type {
  CapabilityManifest,
  PortableDossierArtifact,
} from "../lib/api";
import type { CddCase, CddRequest, StoredDocument } from "../lib/types";

interface Persona {
  id: string;
  subject: string;
  tenant: string;
  principals: string;
}

/** A stable, URL-safe case id derived from the subject's name. */
function caseIdFor(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

export function AgentConsole({ embedded = false }: { embedded?: boolean }) {
  return (
    <DocumentViewerProvider>
      <AgentConsoleContent embedded={embedded} />
    </DocumentViewerProvider>
  );
}

function AgentConsoleContent({ embedded }: { embedded: boolean }) {
  const [name, setName] = useState("");
  const [type, setType] = useState<"individual" | "entity">("entity");
  const [jurisdiction, setJurisdiction] = useState("SG");
  const [documents, setDocuments] = useState<StoredDocument[]>([]);
  const [caseData, setCaseData] = useState<CddCase | null>(null);
  const [blocked, setBlocked] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [authRequired, setAuthRequired] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [selectedPersona, setSelectedPersona] = useState("");
  const [profile, setProfile] = useState("");
  const [transportReady, setTransportReady] = useState(false);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);
  // Three states, not two. `undefined` is "not asked yet" and renders nothing;
  // `null` is "asked and could not answer" and MUST render, because a readiness
  // panel that disappears on error reports a demonstration as a production
  // deployment by omission.
  const [capabilityManifest, setCapabilityManifest] =
    useState<CapabilityManifest | null | undefined>(undefined);

  const caseId = caseIdFor(name);
  const loadedCaseId = useRef("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      let status: Awaited<ReturnType<typeof health>>;
      try {
        status = await health();
      } catch (e) {
        if (cancelled) return;
        setTransportReady(false);
        if (e instanceof AuthRequiredError) {
          setAuthRequired(e.returnTo);
        } else {
          setBootstrapError(
            `Doc1 startup failed: ${e instanceof Error ? e.message : String(e)}`,
          );
        }
        return;
      }
      if (cancelled) return;
      configureApiTransport({
        identityMode: status.identity_mode,
        ...(!embedded
          ? {
              onSignal: (signal) => {
                if (signal.type !== "authentication") return;
                setTransportReady(false);
                setAuthRequired(
                  `${window.location.pathname}${window.location.search}`,
                );
              },
            }
          : {}),
      });
      setProfile(status.profile);
      setBootstrapError(null);
      setTransportReady(true);
      try {
        setCapabilityManifest(await capabilities());
      } catch {
        setCapabilityManifest(null);
      }
      if (status.profile !== "local" && status.profile !== "live") return;
      try {
        const list = await listPersonas();
        if (cancelled || list.length === 0) return;
        setPersonas(list);
        setSelectedPersona(list[0].id);
        setDevPersona(list[0].id);
      } catch {
        // Persona picker is dev-only convenience; ignore lookup failures.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [embedded]);

  // Show whatever is already in custody when the analyst names a case, so returning to a
  // subject does not look like an empty case file and re-uploading is not needed.
  useEffect(() => {
    if (!transportReady || !caseId || caseId === loadedCaseId.current) return;
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const existing = await listDocuments(caseId);
        if (cancelled) return;
        loadedCaseId.current = caseId;
        setDocuments(existing);
      } catch {
        // A case with nothing filed yet is the normal starting state, not an error.
      }
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [caseId, transportReady]);

  // A live assessment runs several model calls and takes minutes, so the wait needs a
  // visible clock. Without one, a working build is indistinguishable from a hung one.
  useEffect(() => {
    if (!loading) return;
    const started = Date.now();
    setElapsed(0);
    const timer = setInterval(() => setElapsed(Math.round((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [loading]);

  function onPersonaChange(id: string) {
    setSelectedPersona(id);
    setDevPersona(id);
    // Identity decides what is readable, so re-read the case under the new one.
    loadedCaseId.current = "";
    setDocuments([]);
    setCaseData(null);
  }

  const onDocumentsChange = useCallback((next: StoredDocument[]) => {
    setDocuments(next);
    setCaseData(null); // the dossier no longer reflects the evidence on file
  }, []);

  async function onAssess() {
    setLoading(true);
    setError(null);
    setBlocked(null);
    setCaseData(null);
    setAuthRequired(null);
    const request: CddRequest = {
      subject: { id: caseId, name: name.trim(), type, jurisdiction },
      documents: documents.map((d) => ({
        id: d.id,
        doc_type: d.doc_type,
        acl_tags: [`case:${caseId}`],
      })),
    };
    try {
      const result = await assessCdd(request);
      if (isBlocked(result)) {
        setBlocked(result.detail);
      } else {
        setCaseData(result);
      }
    } catch (e) {
      if (e instanceof AuthRequiredError) {
        // Standalone: request() already triggered a top-level redirect to /auth/login.
        // The current native embed shows its compatibility link here. Future Mode 4/5
        // signals the loader, which renders the separate Mode 6 URL in host DOM.
        setAuthRequired(e.returnTo);
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setLoading(false);
    }
  }

  async function onExport() {
    if (!caseData) return;
    setError(null);
    try {
      const artifact = await exportPortableDossier(caseData);
      const blob = new Blob([JSON.stringify(artifact, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${caseData.id || "cdd-dossier"}.portable.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function onImport(file: File | null) {
    if (!file) return;
    setError(null);
    try {
      const parsed = JSON.parse(await file.text()) as PortableDossierArtifact;
      const dossier = await importPortableDossier(parsed);
      setName(dossier.subject.name);
      setType(dossier.subject.type);
      setJurisdiction(dossier.subject.jurisdiction);
      setCaseData(dossier);
      setBlocked(null);
    } catch (e) {
      setError(
        `Portable dossier could not be loaded: ${
          e instanceof Error ? e.message : String(e)
        }`,
      );
    }
  }

  // Evidence is required in `live`, where the index holds only what this user uploaded.
  // The `local` profile ships a seeded demo corpus, so an assessment there is grounded
  // without an upload. Either way the backend refuses an ungrounded case, so this is a
  // clearer failure, not a weaker rule.
  const needsDocuments = profile === "live" && documents.length === 0;
  const canAssess = transportReady && Boolean(name.trim()) && !needsDocuments && !loading;

  return (
    <div className="space-y-6">
      <CapabilityPanel manifest={capabilityManifest} />

      {!embedded && personas.length > 0 ? (
        <Panel title="Demo identity">
          <label className="text-sm">
            <span className="text-ink-500">Persona</span>
            <select
              className="mt-1 w-full rounded border border-ink-200 px-2 py-1.5 text-sm sm:w-96"
              value={selectedPersona}
              onChange={(e) => onPersonaChange(e.target.value)}
            >
              {personas.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.subject} · {p.tenant}
                </option>
              ))}
            </select>
          </label>
        </Panel>
      ) : null}

      <Panel title="Assess a subject">
        <div className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          <label className="min-w-0 text-sm sm:col-span-2 xl:col-span-2">
            <span className="text-ink-500">Subject name</span>
            <input
              className="mt-1 w-full rounded border border-ink-200 px-2 py-1.5 text-sm"
              data-demo="subject-name"
              placeholder="Legal name of the company or person"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="min-w-0 text-sm">
            <span className="text-ink-500">Type</span>
            <select
              className="mt-1 w-full rounded border border-ink-200 px-2 py-1.5 text-sm"
              value={type}
              onChange={(e) => setType(e.target.value as "individual" | "entity")}
            >
              <option value="entity">entity</option>
              <option value="individual">individual</option>
            </select>
          </label>
          <label className="min-w-0 text-sm">
            <span className="text-ink-500">Jurisdiction</span>
            <input
              className="mt-1 w-full rounded border border-ink-200 px-2 py-1.5 text-sm"
              value={jurisdiction}
              onChange={(e) => setJurisdiction(e.target.value)}
            />
          </label>
        </div>
      </Panel>

      <DocumentPanel
        caseId={caseId}
        documents={documents}
        onChange={onDocumentsChange}
        disabled={loading || !transportReady}
        seededDemoEvidence={profile === "local"}
      />

      <Panel title="Build the dossier">
        <div className="flex flex-wrap items-center gap-3">
          <button
            data-demo="build-dossier"
            className="rounded bg-regblue-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
            onClick={onAssess}
            disabled={!canAssess}
          >
            {loading ? "Assessing…" : "Build CDD dossier"}
          </button>
          {loading ? (
            <span className="text-sm text-ink-500">
              Reading the documents, researching the subject and drafting the dossier.
              {elapsed > 0 ? ` ${elapsed}s elapsed.` : ""}
            </span>
          ) : null}
          {!loading && !name.trim() ? (
            <span className="text-sm text-ink-400">Name the subject to begin.</span>
          ) : null}
          {!loading && name.trim() && documents.length === 0 ? (
            <span className="text-sm text-ink-400">
              {profile === "local"
                ? "Using the bundled fictional evidence corpus; an upload is optional."
                : "Upload at least one document: a dossier is built from evidence, not from a name."}
            </span>
          ) : null}
        </div>
        {profile === "live" ? (
          <p className="mt-3 text-xs text-ink-400">
            Documents are read by a model running on this machine. Only the subject name
            is sent out, for the adverse-media and corporate-registry web searches.
          </p>
        ) : null}
      </Panel>

      {authRequired ? (
        <div className="flex items-center justify-between gap-3 rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
          <span>Sign-in required.</span>
          {!embedded ? (
            <a
              href={`/agent/auth/login?return_to=${encodeURIComponent(authRequired)}`}
              className="inline-flex items-center rounded bg-regblue-600 px-3 py-1.5 text-sm font-medium text-white"
            >
              Sign in
            </a>
          ) : null}
        </div>
      ) : null}

      {!transportReady && !authRequired && !bootstrapError ? (
        <div className="rounded border border-ink-200 bg-ink-50 px-3 py-2 text-sm text-ink-600">
          Connecting to Doc1…
        </div>
      ) : null}

      {bootstrapError ? (
        <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
          {bootstrapError}
        </div>
      ) : null}

      {error ? (
        <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      ) : null}

      {blocked ? (
        <div className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          {blocked}
        </div>
      ) : null}

      {caseData ? (
        <>
          <Panel title="Portable dossier">
            <div className="flex flex-wrap items-center gap-3">
              <button
                className="rounded border border-regblue-600 px-3 py-1.5 text-sm font-medium text-regblue-700"
                onClick={onExport}
              >
                Export open dossier
              </button>
              <label className="cursor-pointer rounded border border-ink-300 px-3 py-1.5 text-sm font-medium text-ink-700">
                Reload exported dossier
                <input
                  className="sr-only"
                  type="file"
                  accept="application/json,.json"
                  onChange={(event) => {
                    void onImport(event.target.files?.[0] ?? null);
                    event.currentTarget.value = "";
                  }}
                />
              </label>
              <span className="text-xs text-ink-500">
                cdd-dossier/v1 · JSON · SHA-256 integrity checked by the API
              </span>
            </div>
          </Panel>
          <CddCaseView caseData={caseData} />
          <UboGraphPanel
            subject={{
              id: caseData.subject.id,
              name: caseData.subject.name,
              type: caseData.subject.type,
              jurisdiction: caseData.subject.jurisdiction,
            }}
          />
          <PerpetualKycPanel
            subject={{
              id: caseData.subject.id,
              name: caseData.subject.name,
              type: caseData.subject.type,
              jurisdiction: caseData.subject.jurisdiction,
            }}
          />
        </>
      ) : null}
    </div>
  );
}
