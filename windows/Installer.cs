using System.Diagnostics;
using NAudio.CoreAudioApi;
namespace GaevAssistant;
sealed class Installer:Form {
 public Installer(){Text="Установка GAEV Assistant";ClientSize=new(660,410);Font=new("Segoe UI",11);StartPosition=FormStartPosition.CenterScreen;BackColor=Color.White;
  var layout=new FlowLayoutPanel{Dock=DockStyle.Fill,Padding=new(24),FlowDirection=FlowDirection.TopDown,WrapContents=false};Controls.Add(layout);
  layout.Controls.Add(new Label{Text="Помощник для звонков",Font=new("Segoe UI Semibold",21),AutoSize=true});
  layout.Controls.Add(new Label{Text="Самостоятельное приложение для Windows 11.\nPython и отдельная служба не нужны.",AutoSize=true,Margin=new(0,10,0,18)});
  var status=new Label{AutoSize=false,Size=new(600,80)};layout.Controls.Add(status);
  void Check(){string cable;try{using var a=MicLease.Cable(DataFlow.Capture);using var b=MicLease.Cable(DataFlow.Render);cable="VB-CABLE установлен.";}catch{cable="VB-CABLE не найден: установите драйвер и перезагрузите Windows.";}status.Text=cable+"\n"+(Process.GetProcessesByName("PhoneExperienceHost").Length>0?"Связь с телефоном запущена. Подключение телефона проверяется в приложении.":"Запустите «Связь с телефоном» и подключите телефон.");}
  var links=new FlowLayoutPanel{Width=600,Height=50};layout.Controls.Add(links);
  void Add(string text,Action action){var b=new Button{Text=text,AutoSize=true,Height=36};b.Click+=(s,e)=>action();links.Controls.Add(b);}
  Add("Скачать VB-CABLE",()=>MainWindow.Open("https://vb-audio.com/Cable/"));Add("Настроить телефон",()=>MainWindow.Open("https://www.microsoft.com/windows/sync-across-your-devices"));Add("Повторить проверку",Check);
  var install=new Button{Text="Установить и создать ярлык",AutoSize=true,Height=45,BackColor=Color.FromArgb(230,240,255)};layout.Controls.Add(install);
  install.Click+=(s,e)=>{try{Install();MessageBox.Show("Установлено. Ярлык «GAEV — помощник для звонков» создан на рабочем столе. Автозагрузка не включена.");Close();}catch(Exception ex){MessageBox.Show("Не удалось установить: "+ex.Message);}};Check();
 }
 public static string Install(){
  var dir=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","GaevAssistant");Directory.CreateDirectory(dir);var target=Path.Combine(dir,"GaevAssistant.exe");var source=Environment.ProcessPath!;if(!string.Equals(source,target,StringComparison.OrdinalIgnoreCase))File.Copy(source,target,true);
  dynamic shell=Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell")!)!;try{dynamic link=shell.CreateShortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),"GAEV — помощник для звонков.lnk"));link.TargetPath=target;link.WorkingDirectory=dir;link.Save();System.Runtime.InteropServices.Marshal.FinalReleaseComObject(link);}finally{System.Runtime.InteropServices.Marshal.FinalReleaseComObject(shell);}return target;
 }
}
