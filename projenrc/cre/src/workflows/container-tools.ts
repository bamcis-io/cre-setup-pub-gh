/**
 * Container tooling shared by both providers' pipelines.
 *
 * Both providers publish images the same way: the unprivileged build runs `build:container-plan`
 * and `build:docker` (kaniko: natively on GitLab's kaniko image, docker-wrapped where a daemon
 * exists, e.g. GitHub runners and laptops) into `assets/containers/<hash>.tar.gz`, which rides in
 * `dist/content.zip`; the privileged deploy pushes those tarballs with crane from `deploy:docker`
 * (inside `deploy:upload`). The deploy never builds an image, so it never runs a Dockerfile
 * from a PR.
 */

/** crane version pinned for reproducible daemonless ECR pushes. */
export const CRANE_VERSION = 'v0.20.2';

/** Download URL of crane for the runner's architecture (`$arch` = x86_64 | arm64, the release's asset names). */
const CRANE_URL = `https://github.com/google/go-containerregistry/releases/download/${CRANE_VERSION}/go-containerregistry_Linux_\${arch}.tar.gz`;

/** Shell that installs crane into `dir` (which must already be on PATH, or be added after). */
export function craneInstall(dir: string): string {
  return [
    // The release names its assets x86_64 and arm64 (amd64 404s): only aarch64 needs renaming.
    'arch=$(uname -m | sed "s/aarch64/arm64/");',
    `mkdir -p "${dir}" &&`,
    `curl -fsSL "${CRANE_URL}" | tar -xz -C "${dir}" crane`,
    `&& "${dir}/crane" version`,
  ].join(' ');
}

/**
 * GitHub step registering QEMU binfmt handlers before a build that makes images, so kaniko can
 * run a Dockerfile's RUN steps for the other architecture (the runners are arm64; an X86_64
 * LoadGenerator image needs emulation, the default ARM64 one doesn't).
 */
export const GITHUB_QEMU_STEP = { name: 'Set up QEMU (cross-arch image builds)', uses: 'docker/setup-qemu-action@v3' };
