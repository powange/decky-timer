import {
  ButtonItem,
  DialogButton,
  Dropdown,
  Field,
  Focusable,
  ModalRoot,
  Navigation,
  PanelSection,
  PanelSectionRow,
  SliderField,
  TextField,
  ToggleField,
  showModal,
  staticClasses,
} from "@decky/ui";
import {
  addEventListener,
  callable,
  definePlugin,
  fetchNoCors,
  removeEventListener,
  toaster,
} from "@decky/api";
import { useEffect, useState, type CSSProperties } from "react";
import { FaStopwatch } from "react-icons/fa";

// --------------------------------------------------------------------------
// Types & backend calls
// --------------------------------------------------------------------------

interface Preset {
  id: string;
  name: string;
  duration: number;
}

interface Timer {
  id: string;
  name: string;
  duration: number;
  preset_id: string | null;
  end_at: number | null;
  remaining: number | null;
  paused: boolean;
}

interface Settings {
  overlay_enabled: boolean;
  position: string;
  size: string;
  opacity: number;
  notify: boolean;
  display: string;
  shortcut: string;
}

interface State {
  now: number;
  presets: Preset[];
  timers: Timer[];
  settings: Settings;
  overlay_running: boolean;
  overlay_error: string;
  controller_status: string;
}

const getState = callable<[], State>("get_state");
const getVersion = callable<[], string>("get_version");
const startTimer = callable<[name: string, duration: number, presetId: string], State>("start_timer");
const pauseTimer = callable<[id: string], State>("pause_timer");
const resumeTimer = callable<[id: string], State>("resume_timer");
const restartTimer = callable<[id: string], State>("restart_timer");
const stopTimer = callable<[id: string], State>("stop_timer");
const stopAll = callable<[], State>("stop_all");
const savePreset = callable<[id: string, name: string, duration: number], State>("save_preset");
const deletePreset = callable<[id: string], State>("delete_preset");
const movePreset = callable<[id: string, delta: number], State>("move_preset");
const saveSettings = callable<[settings: Partial<Settings>], State>("save_settings");

// The backend clock is the reference (the overlay uses it too).
let clockOffset = 0;
// Last state seen from the backend, used by the controller shortcut.
let latestState: State | null = null;
const syncClock = (s: State) => {
  clockOffset = s.now - Date.now() / 1000;
  latestState = s;
  return s;
};
const backendNow = () => Date.now() / 1000 + clockOffset;

const remainingOf = (t: Timer) => (t.paused ? t.remaining ?? 0 : (t.end_at ?? 0) - backendNow());

function formatSeconds(value: number): string {
  let total: number;
  let sign = "";
  if (value > 0) {
    total = Math.ceil(value);
  } else {
    total = Math.floor(-value);
    if (total > 0) sign = "-";
  }
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h ? `${sign}${h}:${pad(m)}:${pad(s)}` : `${sign}${pad(m)}:${pad(s)}`;
}

function formatDuration(seconds: number): string {
  return formatSeconds(seconds);
}

const timerColor = (t: Timer) => (t.paused ? "#ffc84d" : remainingOf(t) <= 0 ? "#ff5c5c" : "#ffffff");

function useTick(ms = 250) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), ms);
    return () => clearInterval(id);
  }, [ms]);
}

// --------------------------------------------------------------------------
// Modals
// --------------------------------------------------------------------------

const modalButtons: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 8,
  marginTop: 16,
};

function ActiveTimerModal({
  timer,
  closeModal,
  onState,
}: {
  timer: Timer;
  closeModal?: () => void;
  onState: (s: State) => void;
}) {
  const [t, setT] = useState<Timer>(timer);
  useTick();

  const run = async (fn: () => Promise<State>, close = false) => {
    const s = syncClock(await fn());
    onState(s);
    const updated = s.timers.find((x) => x.id === t.id);
    if (close || !updated) closeModal?.();
    else setT(updated);
  };

  return (
    <ModalRoot onCancel={closeModal}>
      <div style={{ textAlign: "center" }}>
        <div style={{ fontSize: 18, color: "#ccc" }}>{t.name}</div>
        <div style={{ fontSize: 48, fontWeight: "bold", fontFamily: "monospace", color: timerColor(t) }}>
          {formatSeconds(remainingOf(t))}
        </div>
        <div style={{ fontSize: 12, color: "#999" }}>
          Durée : {formatDuration(t.duration)}
          {t.paused ? " · en pause" : remainingOf(t) <= 0 ? " · temps dépassé" : ""}
        </div>
      </div>
      <Focusable style={modalButtons}>
        {t.paused ? (
          <DialogButton onClick={() => run(() => resumeTimer(t.id), true)}>Reprendre</DialogButton>
        ) : (
          <DialogButton onClick={() => run(() => pauseTimer(t.id), true)}>Pause</DialogButton>
        )}
        <DialogButton onClick={() => run(() => stopTimer(t.id), true)}>Arrêter</DialogButton>
        <DialogButton onClick={() => run(() => restartTimer(t.id), true)}>Relancer depuis le début</DialogButton>
        <DialogButton onClick={closeModal}>Fermer</DialogButton>
      </Focusable>
    </ModalRoot>
  );
}

// Most urgent first: overtime (most overdue first), then running by time
// left, paused last.
const byUrgency = (a: Timer, b: Timer) => {
  const rank = (t: Timer) => (t.paused ? 2 : remainingOf(t) <= 0 ? 0 : 1);
  return rank(a) - rank(b) || remainingOf(a) - remainingOf(b);
};

function TimerPickerModal({
  closeModal,
  onState,
}: {
  closeModal?: () => void;
  onState: (s: State) => void;
}) {
  useTick();
  const timers = [...(latestState?.timers ?? [])].sort(byUrgency);

  return (
    <ModalRoot onCancel={closeModal}>
      <div style={{ fontSize: 18, fontWeight: "bold", marginBottom: 8 }}>Timers en cours</div>
      <Focusable style={modalButtons}>
        {timers.map((t) => (
          <DialogButton
            key={t.id}
            onClick={() => {
              closeModal?.();
              showModal(<ActiveTimerModal timer={t} onState={onState} />);
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span>
                {t.paused ? "⏸ " : ""}
                {t.name}
              </span>
              <span style={{ fontFamily: "monospace", fontWeight: "bold", color: timerColor(t) }}>
                {formatSeconds(remainingOf(t))}
              </span>
            </div>
          </DialogButton>
        ))}
        <DialogButton onClick={closeModal}>Fermer</DialogButton>
      </Focusable>
    </ModalRoot>
  );
}

// --------------------------------------------------------------------------
// Controller shortcut
// --------------------------------------------------------------------------

// The combo is detected by the backend, which reads the Deck controller
// directly (hidraw), like decky-macro. Keys must match SHORTCUTS in main.py.
const SHORTCUTS: Record<string, string> = {
  l5_r5: "L5 + R5",
  l4_r4: "L4 + R4",
  select_start: "Select + Start",
};

const shortcutOptions = [
  { data: "off", label: "Désactivé" },
  ...Object.entries(SHORTCUTS).map(([data, label]) => ({ data, label })),
];

async function openQuickControls() {
  let state: State;
  try {
    state = syncClock(await getState());
  } catch {
    return;
  }
  if (state.timers.length === 0) {
    toaster.toast({ title: "Decky Timer", body: "Aucun timer en cours." });
    return;
  }
  // Bring up the Steam overlay so the dialog is visible on top of the game.
  Navigation.OpenQuickAccessMenu(999 as any); // QuickAccessTab.Decky
  const onState = (s: State) => syncClock(s);
  setTimeout(() => {
    if (state.timers.length === 1) {
      showModal(<ActiveTimerModal timer={state.timers[0]} onState={onState} />);
    } else {
      showModal(<TimerPickerModal onState={onState} />);
    }
  }, 300);
}

function TimerEditorModal({
  preset,
  presetCount,
  closeModal,
  onState,
}: {
  preset?: Preset;
  presetCount: number;
  closeModal?: () => void;
  onState: (s: State) => void;
}) {
  const initial = preset?.duration ?? 300;
  const [name, setName] = useState(preset?.name ?? "");
  const [h, setH] = useState(Math.floor(initial / 3600));
  const [m, setM] = useState(Math.floor((initial % 3600) / 60));
  const [s, setS] = useState(initial % 60);
  const duration = h * 3600 + m * 60 + s;
  const label = name.trim() || "Timer";

  const run = async (fn: () => Promise<State>) => {
    onState(syncClock(await fn()));
  };

  const save = () => run(() => savePreset(preset?.id ?? "", label, duration));
  const launch = () => run(() => startTimer(label, duration, preset?.id ?? ""));

  return (
    <ModalRoot onCancel={closeModal}>
      <div style={{ fontSize: 18, fontWeight: "bold", marginBottom: 8 }}>
        {preset ? "Modifier le timer" : "Nouveau timer"}
      </div>
      <TextField label="Nom" value={name} onChange={(e) => setName(e.target.value)} />
      <SliderField label="Heures" value={h} min={0} max={12} step={1} showValue onChange={setH} />
      <SliderField label="Minutes" value={m} min={0} max={59} step={1} showValue onChange={setM} />
      <SliderField label="Secondes" value={s} min={0} max={59} step={1} showValue onChange={setS} />
      <div style={{ textAlign: "center", fontSize: 22, fontFamily: "monospace", marginTop: 8 }}>
        {formatDuration(duration)}
      </div>
      <Focusable style={modalButtons}>
        {preset ? (
          <>
            <DialogButton disabled={duration <= 0} onClick={async () => { await save(); closeModal?.(); }}>
              Enregistrer
            </DialogButton>
            <DialogButton disabled={duration <= 0} onClick={async () => { await save(); await launch(); closeModal?.(); }}>
              Enregistrer et lancer
            </DialogButton>
            <Focusable style={{ display: "flex", gap: 8 }}>
              <DialogButton style={{ flex: 1 }} onClick={() => run(() => movePreset(preset.id, -1))}>
                Monter
              </DialogButton>
              <DialogButton style={{ flex: 1 }} onClick={() => run(() => movePreset(preset.id, 1))}>
                Descendre
              </DialogButton>
            </Focusable>
            <DialogButton onClick={async () => { await run(() => deletePreset(preset.id)); closeModal?.(); }}>
              Supprimer
            </DialogButton>
          </>
        ) : (
          <>
            <DialogButton disabled={duration <= 0} onClick={async () => { await save(); await launch(); closeModal?.(); }}>
              Enregistrer et lancer
            </DialogButton>
            <DialogButton disabled={duration <= 0} onClick={async () => { await save(); closeModal?.(); }}>
              Enregistrer seulement
            </DialogButton>
            <DialogButton disabled={duration <= 0} onClick={async () => { await launch(); closeModal?.(); }}>
              Lancer sans enregistrer
            </DialogButton>
          </>
        )}
        <DialogButton onClick={closeModal}>Annuler</DialogButton>
      </Focusable>
      {presetCount === 0 && !preset && (
        <div style={{ fontSize: 11, color: "#999", marginTop: 8 }}>
          Un timer enregistré devient un bouton dans le menu pour le relancer en un clic.
        </div>
      )}
    </ModalRoot>
  );
}

// --------------------------------------------------------------------------
// Updates (same mechanism as powange/decky-macro)
// --------------------------------------------------------------------------

// Releases are published by .github/workflows/release.yml.
const UPDATE_REPO = "powange/decky-timer";
const UPDATE_ASSET = "decky-timer.zip";
// Must match "name" in plugin.json so Decky replaces the installed plugin.
const PLUGIN_NAME = "Decky Timer";
// Decky's PluginInstallType.UPDATE
const INSTALL_TYPE_UPDATE = 2;

let updateCheckedThisSession = false;

function UpdateSection() {
  const [current, setCurrent] = useState<string>("");
  const [latest, setLatest] = useState<{ tag: string; url: string } | null>(null);
  const [status, setStatus] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);

  const check = async (version: string, silent = false) => {
    setBusy(true);
    if (!silent) setStatus("Vérification sur GitHub…");
    setLatest(null);
    try {
      const res = await fetchNoCors(`https://api.github.com/repos/${UPDATE_REPO}/releases/latest`, {
        headers: { Accept: "application/vnd.github+json" },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const rel = await res.json();
      const asset = (rel.assets || []).find((a: any) => a.name === UPDATE_ASSET);
      if (!asset) throw new Error(`pas de ${UPDATE_ASSET} dans la dernière release`);
      if (rel.tag_name === `v${version}`) {
        setStatus(`À jour (${rel.tag_name})`);
      } else {
        setLatest({ tag: rel.tag_name, url: asset.browser_download_url });
        setStatus(`Mise à jour disponible : ${rel.tag_name}`);
      }
    } catch (e: any) {
      if (!silent) setStatus(`Échec de la vérification : ${e?.message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    getVersion()
      .then((v) => {
        setCurrent(v);
        // One silent check per plugin load, so an update shows up by itself.
        if (!updateCheckedThisSession) {
          updateCheckedThisSession = true;
          check(v, true);
        }
      })
      .catch(() => setCurrent("?"));
  }, []);

  const install = async () => {
    if (!latest) return;
    const backend = (window as any).DeckyBackend;
    if (!backend?.call) {
      setStatus("API d'installation de Decky indisponible – installe depuis l'URL");
      return;
    }
    // Opens Decky's own install confirmation, like the store does.
    await backend.call(
      "utilities/install_plugin",
      latest.url,
      PLUGIN_NAME,
      latest.tag.replace(/^v/, ""),
      "",
      INSTALL_TYPE_UPDATE
    );
  };

  return (
    <PanelSection title="Mises à jour">
      <PanelSectionRow>
        <div style={{ fontSize: 12, color: "#bbb" }}>
          Installée : <b>{current || "…"}</b>
          {status && <div>{status}</div>}
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        {latest ? (
          <ButtonItem layout="below" onClick={install}>
            Mettre à jour vers {latest.tag}
          </ButtonItem>
        ) : (
          <ButtonItem layout="below" onClick={() => check(current)} disabled={busy}>
            Rechercher une mise à jour
          </ButtonItem>
        )}
      </PanelSectionRow>
    </PanelSection>
  );
}

// --------------------------------------------------------------------------
// Main panel
// --------------------------------------------------------------------------

const positionOptions = [
  { data: "top-left", label: "En haut à gauche" },
  { data: "top-center", label: "En haut au centre" },
  { data: "top-right", label: "En haut à droite" },
  { data: "bottom-left", label: "En bas à gauche" },
  { data: "bottom-right", label: "En bas à droite" },
];

const sizeOptions = [
  { data: "small", label: "Petite" },
  { data: "medium", label: "Moyenne" },
  { data: "large", label: "Grande" },
  { data: "xlarge", label: "Très grande" },
];

function Content() {
  const [state, setState] = useState<State | null>(null);
  const [editMode, setEditMode] = useState(false);
  useTick();

  const apply = (s: State) => setState(syncClock(s));
  const refresh = () => getState().then(apply).catch(() => {});

  useEffect(() => {
    refresh();
    const onFinished = () => refresh();
    const listener = addEventListener("timer_finished", onFinished);
    return () => removeEventListener("timer_finished", listener);
  }, []);

  if (!state) {
    return (
      <PanelSection>
        <PanelSectionRow>Chargement…</PanelSectionRow>
      </PanelSection>
    );
  }

  const { timers, presets, settings } = state;
  const updateSettings = async (patch: Partial<Settings>) => {
    setState({ ...state, settings: { ...settings, ...patch } });
    apply(await saveSettings(patch));
  };

  const openActive = (t: Timer) =>
    showModal(<ActiveTimerModal timer={t} onState={apply} />);
  const openEditor = (p?: Preset) =>
    showModal(<TimerEditorModal preset={p} presetCount={presets.length} onState={apply} />);

  return (
    <>
      {timers.length > 0 && (
        <PanelSection title="En cours">
          {timers.map((t) => (
            <PanelSectionRow key={t.id}>
              <ButtonItem layout="below" onClick={() => openActive(t)}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }}>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {t.paused ? "⏸ " : ""}
                    {t.name}
                  </span>
                  <span style={{ fontFamily: "monospace", fontWeight: "bold", color: timerColor(t), marginLeft: 8 }}>
                    {formatSeconds(remainingOf(t))}
                  </span>
                </div>
              </ButtonItem>
            </PanelSectionRow>
          ))}
          {timers.length > 1 && (
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={async () => apply(await stopAll())}>
                Tout arrêter
              </ButtonItem>
            </PanelSectionRow>
          )}
        </PanelSection>
      )}

      <PanelSection title="Mes timers">
        {presets.map((p) => {
          const running = timers.some((t) => t.preset_id === p.id);
          return (
            <PanelSectionRow key={p.id}>
              <ButtonItem
                layout="below"
                onClick={async () =>
                  editMode ? openEditor(p) : apply(await startTimer(p.name, p.duration, p.id))
                }
              >
                <div style={{ display: "flex", justifyContent: "space-between", width: "100%" }}>
                  <span>
                    {editMode ? "✎ " : "▶ "}
                    {p.name}
                    {running && !editMode ? " •" : ""}
                  </span>
                  <span style={{ fontFamily: "monospace", color: "#aaa", marginLeft: 8 }}>
                    {formatDuration(p.duration)}
                  </span>
                </div>
              </ButtonItem>
            </PanelSectionRow>
          );
        })}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => openEditor()}>
            + Nouveau timer
          </ButtonItem>
        </PanelSectionRow>
        {presets.length > 0 && (
          <PanelSectionRow>
            <ToggleField
              label="Modifier mes timers"
              description={editMode ? "Touche un timer pour le modifier, le déplacer ou le supprimer" : undefined}
              checked={editMode}
              onChange={setEditMode}
            />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Overlay en jeu">
        <PanelSectionRow>
          <ToggleField
            label="Afficher l'overlay"
            checked={settings.overlay_enabled}
            onChange={(v) => updateSettings({ overlay_enabled: v })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Position" childrenContainerWidth="max">
            <Dropdown
              rgOptions={positionOptions}
              selectedOption={settings.position}
              onChange={(o) => updateSettings({ position: o.data })}
            />
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Taille" childrenContainerWidth="max">
            <Dropdown
              rgOptions={sizeOptions}
              selectedOption={settings.size}
              onChange={(o) => updateSettings({ size: o.data })}
            />
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Opacité du fond"
            value={Math.round(settings.opacity * 100)}
            min={20}
            max={100}
            step={5}
            showValue
            onChange={(v) => updateSettings({ opacity: v / 100 })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Notification à la fin"
            checked={settings.notify}
            onChange={(v) => updateSettings({ notify: v })}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Raccourci manette">
        <PanelSectionRow>
          <Field label="Combinaison" childrenContainerWidth="max">
            <Dropdown
              rgOptions={shortcutOptions}
              selectedOption={settings.shortcut ?? "l5_r5"}
              onChange={(o) => updateSettings({ shortcut: o.data })}
            />
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={{ fontSize: 12, color: "#999", lineHeight: 1.4 }}>
            Maintiens la combinaison en jeu pour mettre en pause, relancer ou arrêter un timer.
            <div style={{ color: state.controller_status === "Manette détectée" ? "#7c7" : "#ff8080" }}>
              {state.controller_status}
            </div>
          </div>
        </PanelSectionRow>
        {state.overlay_error && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: "#ff8080" }}>
              {state.overlay_error}
              <div style={{ color: "#999" }}>Change un réglage pour réessayer. Détails : logs du plugin (overlay.log).</div>
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      <UpdateSection />
    </>
  );
}

export default definePlugin(() => {
  // Global listener (works even when the menu is closed, e.g. in game).
  const listener = addEventListener<[{ id: string; name: string }]>("timer_finished", (t) => {
    toaster.toast({
      title: `⏰ ${t?.name ?? "Timer"} terminé`,
      playSound: true,
      showToast: true,
      body: SHORTCUTS[latestState?.settings.shortcut ?? ""]
        ? `Le temps continue en négatif – maintiens ${SHORTCUTS[latestState!.settings.shortcut]} pour l'arrêter.`
        : "Le temps continue en négatif – ouvre le menu pour l'arrêter.",
    });
  });

  // Load settings now so the shortcut works before the menu is ever opened.
  getState().then(syncClock).catch(() => {});
  const shortcutListener = addEventListener("open_quick_controls", () => {
    openQuickControls();
  });

  return {
    name: "Decky Timer",
    titleView: <div className={staticClasses.Title}>Decky Timer</div>,
    content: <Content />,
    icon: <FaStopwatch />,
    onDismount() {
      removeEventListener("timer_finished", listener);
      removeEventListener("open_quick_controls", shortcutListener);
    },
  };
});
