using TextBox = System.Windows.Controls.TextBox;
using System.Windows.Input;
using System.Windows.Threading;

namespace Himari.Companion;

// Preview runs before TextBox's AcceptsReturn handling. IME owns composition keys.
internal sealed class ComposerInput
{
    private bool composing;
    private int compositionVersion;
    internal ComposerInput(TextBox input, Action send, Func<ModifierKeys>? modifiers = null)
    {
        input.PreviewKeyDown += (_, e) =>
        {
            // A key the IME consumes (a Japanese conversion's Enter) arrives as ImeProcessed and
            // is left alone. Korean IME commits the last syllable and passes this same Enter on,
            // so a real Enter never reaches the multiline TextBox: during a composition it sends
            // once the commit has landed.
            if (e.Key != Key.Enter || (modifiers?.Invoke() ?? Keyboard.Modifiers).HasFlag(ModifierKeys.Shift)) return;
            e.Handled = true;
            if (e.IsRepeat) return;
            if (composing) input.Dispatcher.BeginInvoke(DispatcherPriority.Background, send);
            else send();
        };
        input.AddHandler(TextCompositionManager.PreviewTextInputStartEvent, new TextCompositionEventHandler((_, e) =>
        {
            // Ordinary characters auto-complete immediately; they must not delay
            // the next Enter. IME/FrameworkTextComposition uses AutoComplete.Off.
            if (e.TextComposition.AutoComplete != TextCompositionAutoComplete.Off) return;
            composing = true; compositionVersion++;
        }), true);
        input.AddHandler(TextCompositionManager.PreviewTextInputEvent, new TextCompositionEventHandler((_, _) =>
        {
            if (!composing) return;
            int version = compositionVersion;
            // Still composing for the Enter which commits this composition: it sends after the commit.
            input.Dispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() => { if (version == compositionVersion) composing = false; }));
        }), true);
        input.LostKeyboardFocus += (_, _) => { composing = false; compositionVersion++; };
    }
}
