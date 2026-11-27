# decky-timer – plugin Decky Loader

Des timers nommés pour le Steam Deck, affichés **en overlay par-dessus le jeu**.

![Aperçu de l'overlay](assets/overlay-preview.png)

## Fonctionnalités

- **Timers enregistrés** : donne un nom et une durée à un timer, il devient un bouton dans le menu. Un appui le relance, sans liste déroulante.
- **Timers ponctuels** : lance un timer sans l'enregistrer.
- **Plusieurs timers en même temps**, empilés dans l'overlay.
- **Temps dépassé** : à zéro, le timer continue en négatif (en rouge) et une notification s'affiche.
- **Alarme sonore** : un bip d'alarme dédié retentit à la fin, puis se répète (toutes les 10 à 60 s) tant que le timer est en négatif et n'est ni en pause ni arrêté. Volume réglable et bouton de test. Le son est généré par le plugin et joué via PipeWire (`pw-play`).
- **Contrôle** : touche un timer en cours pour le mettre en pause, le reprendre, ajouter ou retirer 1 ou 5 min, le relancer, l'arrêter, couper son alarme ou le reporter (« Rappel dans 5 min »).
- **Raccourci manette** : maintiens une combinaison en jeu (L5 + R5 par défaut, ou L4 + R4, Select + Start) pour ouvrir directement le menu pause / relancer / arrêter. S'il y a plusieurs timers, une liste triée par urgence s'affiche. Comme decky-macro, la manette est lue directement (hidraw), donc ça marche en jeu, et le plugin tourne en root pour y accéder. Toucher l'overlay lui-même n'est pas possible : gamescope envoie le tactile au jeu.
- **Overlay en jeu** via [Decky Overlay](https://github.com/powange/decky-overlay) : position (en haut au centre par défaut, pour ne pas passer sous le menu d'accès rapide), affichage seulement les N dernières minutes, on/off, aperçu de 10 s. Taille et opacité se règlent dans Decky Overlay.
- **Saisie rapide** : durées toutes prêtes (1, 5, 10, 15, 30 min), confirmation avant de lancer un second exemplaire d'un timer déjà en cours, temps restant affiché sur son bouton.
- **Mise à jour intégrée** : le plugin vérifie la dernière release GitHub et l'installe via l'installeur de Decky. Plus besoin de retaper l'URL.

## Installation

1. Dans Decky : *Paramètres → Développeur*, active le mode développeur.
2. *Installer un plugin depuis une URL* :
   ```
   https://github.com/powange/decky-timer/releases/latest/download/decky-timer.zip
   ```
3. Ensuite, les mises à jour se font avec le bouton **Mises à jour** en bas du plugin.

## Comment marche l'overlay

gamescope n'affiche qu'une seule fenêtre d'overlay externe à la fois : deux plugins qui ouvrent chacun la leur se gênent. L'affichage en jeu est donc confié au plugin [Decky Overlay](https://github.com/powange/decky-overlay), partagé par plusieurs plugins. Decky Timer lui envoie ses timers par un socket local, sous forme de comptes à rebours, et c'est Decky Overlay qui les dessine et les fait défiler chaque seconde.

Decky ne gère pas les dépendances entre plugins. Si Decky Overlay manque, Decky Timer propose de l'installer une fois, au premier lancement : Decky affiche sa confirmation habituelle. Un bouton « Installer Decky Overlay » reste disponible dans le panneau. Sans lui, les timers, le raccourci, l'alarme et les notifications fonctionnent quand même, juste sans affichage en jeu.

## Fiabilité et sécurité

- **Mise en veille** : les timers suivent l'heure réelle. Le temps passé en veille ou Deck éteint compte, et un timer expiré entre-temps apparaît en négatif au réveil (l'alarme ne sonne pas s'il a dépassé l'arrêt automatique). Si l'horloge est corrigée au réveil (synchronisation réseau), les timers en cours sont décalés d'autant.
- **Droits root** : nécessaires pour lire la manette. L'overlay et l'alarme tournent sous l'utilisateur normal. Toutes les valeurs reçues du panneau ou relues depuis `state.json` sont validées (liste blanche de réglages, bornes sur les durées), et aucune commande shell n'est lancée.
- **Sauvegarde** : `state.json` est écrit de façon atomique et synchronisé sur le disque. Un fichier abîmé est réparé au chargement au lieu de faire planter le plugin.
- **Mise à jour intégrée** : elle passe par une API interne de Decky. Si une version de Decky la casse, le panneau affiche l'URL d'installation manuelle.

## Dépannage

- Si l'alarme ne sonne pas, une erreur s'affiche dans la section Alarme, avec le chemin exact de `alarm.log`. Pour l'overlay, le panneau indique si Decky Overlay est connecté, installé mais inactif, ou absent. Ses propres erreurs s'affichent dans le panneau de Decky Overlay.
- L'affichage X utilisé est `:0` par défaut. Il peut être changé dans `~/homebrew/settings/Decky Timer/state.json` (clé `display`).

## Développement

```bash
pnpm install
pnpm run build        # dist/index.js
pnpm run package      # target/decky-timer/
```

Tests : `pip install pytest && python -m pytest tests` (les tests d'overlay utilisent Xvfb s'il est installé). La CI lance les tests et la vérification de types à chaque push et pull request, et ne publie une release que s'ils passent.

Pour publier une release, augmente `version` dans `package.json` et pousse sur `main`. Le workflow `release.yml` construit le zip et crée la release `v<version>`.

## Licence

BSD-3-Clause
