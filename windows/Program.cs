namespace GaevAssistant;
static class Program {
 [STAThread] static void Main(string[] args){
  if(args.Contains("--install")||Path.GetFileNameWithoutExtension(Environment.ProcessPath??"").EndsWith("-Setup",StringComparison.OrdinalIgnoreCase)){ApplicationConfiguration.Initialize();Application.Run(new Installer());return;}
  if(args.Contains("--install-local")){Installer.Install();return;}
  if(args.Contains("--self-test")){try{SelfTest.Run().GetAwaiter().GetResult();}catch(Exception e){Directory.CreateDirectory(Data.Root);File.WriteAllText(Data.FileAt("self-test.txt"),"FAIL: "+e);Environment.ExitCode=1;}return;}
  if(args.Contains("--diagnostic")){try{Data.Initialize();var p=Phone.Read(CancellationToken.None).GetAwaiter().GetResult();File.WriteAllText(Data.FileAt("diagnostic.txt"),"Phone: "+p.Phase);}catch(Exception e){File.WriteAllText(Data.FileAt("diagnostic.txt"),e.ToString());}return;}
  ApplicationConfiguration.Initialize();using var mutex=new Mutex(true,"Local\\GaevStandaloneAssistant",out var created);if(!created){MessageBox.Show("Помощник уже запущен. Откройте его значок возле часов.");return;}try{Data.Initialize();ContactStore.Import();MicLease.Recover();Application.Run(new MainWindow());}catch(Exception e){MessageBox.Show("Не удалось запустить приложение: "+e.Message);}
 }
}
