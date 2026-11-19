package space.nbtca.huaji;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import net.minecraft.server.level.ServerPlayer;
import net.minecraftforge.common.MinecraftForge;
import net.minecraftforge.event.TickEvent;
import net.minecraftforge.fml.common.Mod;

@Mod("huajiplayerstatus")
public final class PlayerStatus {
    private static final Path DIRECTORY = Path.of("huaji-player-status");
    private static final Gson JSON = new Gson();
    private int ticks;
    private boolean writeFailed;

    public PlayerStatus() {
        MinecraftForge.EVENT_BUS.addListener(this::onServerTick);
    }

    private void onServerTick(TickEvent.ServerTickEvent event) {
        if (event.phase != TickEvent.Phase.END || ++ticks < 100) {
            return;
        }
        ticks = 0;
        JsonArray players = new JsonArray();
        for (ServerPlayer player : event.getServer().m_6846_().m_11314_()) {
            var profile = player.m_36316_();
            JsonObject row = new JsonObject();
            row.addProperty("name", profile.getName());
            row.addProperty("uuid", profile.getId().toString());
            row.addProperty("ping", player.f_8943_);
            row.addProperty("world", player.m_284548_().m_46472_().m_135782_().toString());
            players.add(row);
        }
        JsonObject report = new JsonObject();
        report.addProperty("timestamp", System.currentTimeMillis() / 1000);
        report.add("players", players);
        try {
            Files.createDirectories(DIRECTORY);
            Path temporary = DIRECTORY.resolve("players.tmp");
            Files.writeString(temporary, JSON.toJson(report));
            Files.move(temporary, DIRECTORY.resolve("players.json"),
                    StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            writeFailed = false;
        } catch (IOException e) {
            if (!writeFailed) {
                System.getLogger("HuaJiPlayerStatus").log(System.Logger.Level.WARNING,
                        "Cannot publish online player snapshot", e);
            }
            writeFailed = true;
        }
    }
}
