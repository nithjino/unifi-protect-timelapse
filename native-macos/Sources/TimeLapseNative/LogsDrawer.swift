import SwiftUI

struct LogsDrawer: View {
    @ObservedObject var model: AppModel

    var body: some View {
        GroupBox {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        if model.logs.isEmpty {
                            Text("Application activity and errors will appear here.")
                                .foregroundStyle(.secondary)
                        } else {
                            Text(model.logs.map(\.line).joined(separator: "\n"))
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        Color.clear
                            .frame(height: 0)
                            .id("log-bottom")
                    }
                    .padding(6)
                }
                .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 5))
                .onChange(of: model.logs.count) { _, _ in
                    proxy.scrollTo("log-bottom", anchor: .bottom)
                }
            }
        } label: {
            HStack {
                Text("Application Logs")
                Spacer()
                Button("Clear") { model.clearLogs() }
                    .controlSize(.small)
            }
        }
    }
}
