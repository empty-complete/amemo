import { Button } from "@heroui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { Bridge, OrcaInstall } from "./bridge";

export function OrcaSetup({ bridge }: { bridge: Bridge }) {
  const { t } = useTranslation();
  const [result, setResult] = useState<OrcaInstall | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const install = async () => {
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      setResult(await bridge.installOrca(result?.status === "approval" ? result.fingerprint : null));
    } catch (e) {
      setResult(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPending(false);
    }
  };

  if (result?.status === "installed") {
    return <p role="status">{t("orca.installed")}</p>;
  }

  return (
    <div className="wz__checks">
      {result?.status === "approval" && (
        <div>
          <p className="wz__row-title">{t("orca.permission")}</p>
          <p className="wz__meta" lang="en">
            {result.access}
          </p>
          <p className="wz__meta">{t("orca.access")}</p>
        </div>
      )}
      {error && <p className="wz__error" role="alert">{error}</p>}
      <Button variant="outline" isPending={pending} onPress={() => void install()}>
        {t(result?.status === "approval" ? "orca.approve" : "orca.prepare")}
      </Button>
    </div>
  );
}
