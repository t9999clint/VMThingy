# Script Thingies
There are seveeral scripts planned for this project. At the moment only two are shared here, VMThingy and SMPT.

Planned scripts

- **StateThingy**: Script system for runing other scripts depending on the state of your PC. (Locked, Unlocked, Active Network device, Current Subnet, etc). Only Locked/Unlocked is finished right now.
- **InstallThingy**: Give it an installer file (any OS) and it'll install it for you. Otherwise it will act as a installer meta search engine. (update parts are just using the ujust update thing)
- **RetroThingy**: Give it a rom/iso/exe file and it'll install/configure/start the appropriate emulator. Otherwise it lists a bunch of consoles, then you tell it what to open. (99% of it is just RetroArch and MAME)
- **VideoThingy**: GUI for transcoding/capturing video using ffmpeg. Has lowlatency video preview mode.
- **FlatThingy**: Flathub CLI wrapper tool to make flathub cli commands easier.
- **GameThingy**: Gamescope/LSFG/GOverlay wrapper frontend tool for launching games with any combination of those tools. Will remember settings for each Game (Easy CRT Filters and such). Goal for this one is to be a mostly feature complete, Linux equivalent to Lossless scaling.

--------------
**SMPT - The Simple Multi Package Tool**

Pronounced Simped, SMPT is used to install packages using whatever package manager it can find.
Basically it's a search tool that will look up the request on all your package sources at once and spit it out into a list for you to choose from.
Currently supports flatpak, appman, brew, cargo, apt, dnf, pacman and rpm-ostree.

Each package source is configurable through the config file at ~/.config/smpt/config.ini (will first check it's own directory for a config.ini to allow for portable use)

To install, just download the smpt.py file, and run `python ./smpt.py deploy`
You can also just use it as it is without installing it by marking it as executable and calling it directly.

--------------
**VMThingy - The simple Windows VM starting tool for Linux.**

This tool is meant to make it easy to start a Windows VM and connect to it through RDP.

Logic steps of program:
- It will auto-detect what VM software you have, what VMs you have set up already, and then which ones of those run Windows.
- If there is more than one Windows VM detected, it will give you a list of VMs to connect to.
- Then VMThingy will start the VM if it's stopped and wait for the VM's IP address to become availible, and then for the RDP port to become active.
- When the RDP is ready it will then start KRDC and tell it to connect to the VM's IP address using the host's username.

Right now it only works with QEMU-Libvirt (Virt-Manager), but VMWare is in the works. After that I'll probably add Virtualbox.
In theory this script should work with any distro, but I've only tested it with Bazzite atm.

There's some other features I'd like to add, like LookingGlass support, and support for RemoteApps (Like WinBoat).
Once all of that is done I might add other VM scripts, (setting up PCI passthrough, downloading/patching Windows ISOs, etc

--------------
**How to Install:**

Download the script thingy file you want to use and set it as executable. (chmod +x)
Then just doubleclick it to run it.

--------------
**AI Disclaimer:**

These scripts are about 80% written by ChatGPT and claude, but every line was inspected and tested before being added to the repo. If I don't understand what it's doing, then it's not getting added.

I see AI as a tool for coding faster by getting letting it do the grunt work. It is NOT capible of writing code on it's own. I usually only give it a function to write at a time. This helps keep it's halucinations down to a minimum.
