# MS-10 Rules

A configurable mouse button remapper for Linux.

MS-10 Rules lets you customize your mouse buttons with flexible actions, triggers and profiles through a graphical interface.

> [!IMPORTANT]
> If the installer is taking too long, DO NOT cancel it. It may take long but it works.

## Features

- Custom mouse button assignments
- Multiple trigger modes
- While Holding and Toggle behaviors
- Keyboard shortcuts
- Command execution
- Application and URL launching
- Vertical and horizontal scrolling
- Adjustable sniper mode
- Multiple actions per assignment
- Receiver selection
- Profile management
- Systemd service integration

## Requirements

- Linux
- Python 3
- GTK 4
- PyGObject
- python-evdev
- systemd
- udev
- ACL support

## Installation

Clone the repository:

    git clone https://github.com/batata123muitoboa-dot/ms10-rules.git
    cd ms10-rules

Run the installer:

    ./install.sh

The installer checks for required dependencies and configures the application.

If dependencies are missing, install the reported packages using your distribution's package manager and run the installer again.

## Usage

Launch MS-10 Rules from your desktop application menu.

Select your mouse receiver, create assignments and configure the actions and triggers you want.

## Triggers

### Click

Executes an action when the assigned button is clicked.

### While Holding

Activates a supported action while the physical button remains pressed. Releasing the button deactivates the action.

### Toggle

Switches an action between active and inactive states with successive activations.

### Hold

Activates an action after the configured hold duration.

## Actions

MS-10 Rules supports configurable actions such as:

- Mouse button remapping
- Keyboard shortcuts
- Command execution
- Application launching
- URL opening
- Media controls
- Scrolling
- Button disabling

## Architecture

MS-10 Rules consists of a graphical GTK4 application and a separate input service.

The graphical application manages configurations, assignments and profiles. The input service processes mouse events and emits virtual input events through Linux input interfaces.

## Service Management

Check the service status:

    systemctl --user status ms10-rules

Restart the service:

    systemctl --user restart ms10-rules

Follow service logs:

    journalctl --user -u ms10-rules -f

## Troubleshooting

### Mouse not detected

Check the receiver connection and select the correct device in the application.

### Assignments not responding

Verify the selected receiver and restart the input service.

### Permission issues

Make sure the installer configured the required udev rules and device permissions.

## License

MS-10 Rules is licensed under the Apache-2.0 License.

See the LICENSE file for the complete license text.

## Contributing

Bug reports, suggestions and contributions are welcome.

When reporting an issue, include:

- A description of the problem
- Steps to reproduce it
- Relevant service logs
- Your Linux distribution and desktop environment

---

Built for Linux.
