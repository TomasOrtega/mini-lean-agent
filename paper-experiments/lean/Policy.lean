import Lean

open Lean

/-- Parse submitted commands without elaborating or importing candidate code. -/
partial def checkSyntax (stx : Syntax) : IO Unit := do
  if stx.isMissing then
    throw <| IO.userError "Missing syntax in submitted theorem"
  if let .atom _ token := stx then
    if #["sorry", "admit", "axiom", "unsafe", "run_tac", "run_elab", "run_meta",
        "elab", "macro", "initialize", "set_option", "implemented_by"].contains token then
      throw <| IO.userError s!"Disallowed proof syntax: {token}"
  for child in stx.getArgs do
    checkSyntax child

unsafe def main (args : List String) : IO UInt32 := do
  try
    let [prefixPath, candidatePath, challengePath, targetName, reportPath] := args
      | throw <| IO.userError "usage: paper-policy PREFIX CANDIDATE CHALLENGE TARGET REPORT"
    enableInitializersExecution
    initSearchPath (← findSysroot)
    let trustedSource ← IO.FS.readFile prefixPath
    let candidate ← IO.FS.readFile candidatePath
    let challenge ← IO.FS.readFile challengePath
    unless trustedSource.isPrefixOf candidate do
      throw <| IO.userError "The supplied imports, options, and definitions must remain byte-identical"
    unless trustedSource.isPrefixOf challenge do
      throw <| IO.userError "The trusted challenge does not match its prefix"
    let trustedCtx := Parser.mkInputContext trustedSource prefixPath
    let (header, initial, messages) ← Parser.parseHeader trustedCtx
    if messages.hasErrors then
      throw <| IO.userError "Invalid trusted prefix header"
    let (env, messages) ← Elab.processHeader header {} messages trustedCtx (mainModule := `PaperTrustedPrefix)
    messages.forM fun msg => msg.toString >>= IO.eprintln
    let trusted ← Elab.IO.processCommands trustedCtx initial (Elab.Command.mkState env messages {})
    if trusted.commandState.messages.hasErrors then
      trusted.commandState.messages.forM fun msg => msg.toString >>= IO.eprintln
      throw <| IO.userError "Invalid trusted prefix"
    let scope := trusted.commandState.scopes.head!
    let parserCtx : Parser.ParserModuleContext := {
      env := trusted.commandState.env, options := scope.opts,
      currNamespace := scope.currNamespace, openDecls := scope.openDecls
    }
    let tail := String.fromUTF8! (candidate.toUTF8.extract trustedSource.utf8ByteSize candidate.utf8ByteSize)
    let input := Parser.mkInputContext tail candidatePath
    let mut state : Parser.ModuleParserState := {}
    let mut log : MessageLog := {}
    let mut count := 0
    let mut names : Array String := #[]
    let mut checkingSource := trustedSource
    repeat
      let (cmd, next, nextLog) := Parser.parseCommand input parserCtx state log
      state := next
      log := nextLog
      if log.hasErrors then
        throw <| IO.userError "The submitted source does not parse under the trusted imports"
      if cmd.isOfKind ``Parser.Command.eoi then
        break
      unless cmd.isOfKind ``Parser.Command.declaration &&
          cmd[1].isOfKind ``Parser.Command.theorem do
        throw <| IO.userError "Only theorem commands may follow the supplied prefix"
      -- Attributes and declaration modifiers can invoke elaboration extensions.
      unless cmd[0].getArgs.all (fun arg => arg.getArgs.isEmpty) do
        throw <| IO.userError "Theorem attributes and modifiers are not permitted"
      checkSyntax cmd
      let name := cmd[1][1][0].getId.toString
      if names.contains name then
        throw <| IO.userError s!"Duplicate submitted theorem: {name}"
      names := names.push name
      if name == targetName then
        checkingSource := checkingSource ++ String.fromUTF8!
          (challenge.toUTF8.extract trustedSource.utf8ByteSize challenge.utf8ByteSize) ++ "\n"
      else
        unless cmd[1][3].isOfKind ``Parser.Command.declValSimple do
          throw <| IO.userError "Helper theorems require an explicit := proof"
        let some start := cmd.getPos? | throw <| IO.userError "Missing theorem position"
        let some stop := cmd[1][3].getPos? | throw <| IO.userError "Missing proof position"
        let headerSource := String.fromUTF8! (tail.toUTF8.extract start.byteIdx stop.byteIdx)
        checkingSource := checkingSource ++ headerSource ++ ":= by sorry\n"
      count := count + 1
    if count == 0 then
      throw <| IO.userError "The candidate contains no theorem"
    unless names.contains targetName do
      throw <| IO.userError "The candidate omits the registered target"
    IO.FS.writeFile reportPath <| Json.compress <| Json.mkObj [
      ("theorem_names", toJson names), ("challenge_source", toJson checkingSource)
    ]
    IO.println "Source policy accepted"
    return (0 : UInt32)
  catch error =>
    IO.eprintln error.toString
    return (1 : UInt32)
