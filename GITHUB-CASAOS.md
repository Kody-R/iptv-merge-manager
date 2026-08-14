# GitHub + GHCR + CasaOS setup

This is the recommended deployment path for IPTV Merge Manager.

## 1. Create the GitHub repository

Create a repository named:

`iptv-merge-manager`

Upload the contents of this project so `Dockerfile`, `app/`, `requirements.txt`, and `.github/` are at the repository root.

## 2. Push the source

From a computer with Git installed:

```bash
git init
git add .
git commit -m "IPTV Merge Manager v0.3.1"
git branch -M main
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/iptv-merge-manager.git
git push -u origin main
```

The included GitHub Actions workflow publishes a multi-architecture image to:

`ghcr.io/YOUR_GITHUB_USERNAME/iptv-merge-manager`

A push to `main` publishes `latest`. A version tag publishes versioned tags.

## 3. Publish v0.3.1

```bash
git tag v0.3.1
git push origin v0.3.1
```

After the GitHub Actions job finishes, the versioned image is:

`ghcr.io/YOUR_GITHUB_USERNAME/iptv-merge-manager:0.3.1`

## 4. Make the GHCR package public

GitHub Container Registry packages are private when first published unless their visibility is changed. Open the package on GitHub and change its visibility to **Public** if you want CasaOS to pull it without registry credentials.

## 5. Edit the CasaOS compose file

Open `casaos/docker-compose.yml` and replace every occurrence of:

`YOUR_GITHUB_USERNAME`

with your actual GitHub username. Commit and push that change.

## 6. Install in CasaOS

In CasaOS:

1. Open the App Store.
2. Choose **Install a customized app** / compose import.
3. Paste the contents of `casaos/docker-compose.yml`.
4. Install.

The application stores persistent state at:

- `/DATA/AppData/iptv-merge-manager/data`
- `/DATA/AppData/iptv-merge-manager/output`

Open the application at:

`http://CASAOS-IP:8080/`

Generated feeds:

- `http://CASAOS-IP:8080/output/master.m3u`
- `http://CASAOS-IP:8080/output/master.xml`
- `http://CASAOS-IP:8080/output/master.xml.gz`

## Updating later

For the v0.3.4 release (and the same pattern for later releases):

1. Update the source and version.
2. Commit and push.
3. Tag the release:

```bash
git tag v0.3.4
git push origin v0.3.4
```

4. Change the CasaOS image tag from your currently installed version to `0.3.4` and update/recreate the app.

Pinning CasaOS to a versioned image tag is preferred over relying on `latest`, because it makes upgrades explicit and rollbacks straightforward.
