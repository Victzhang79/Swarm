package com.ruoyi.alarm.sdk;

import java.io.IOException;
import java.io.InputStream;
import java.util.Properties;

/**
 * 预警平台 SDK 配置类
 *
 * @author ruoyi
 */
public class AlarmConfig
{
    /** 预警平台服务地址 */
    private String serverUrl;

    /** 应用ID */
    private String appId;

    /** 应用密钥 */
    private String appSecret;

    /** HTTP连接超时（毫秒），默认5000ms */
    private int connectTimeout = 5000;

    /** HTTP读取超时（毫秒），默认10000ms */
    private int readTimeout = 10000;

    /**
     * 从 properties 配置文件加载配置
     *
     * @param classpathPropertiesFile classpath 下的 properties 文件路径
     * @return 配置实例
     */
    public static AlarmConfig load(String classpathPropertiesFile)
    {
        AlarmConfig config = new AlarmConfig();
        Properties props = new Properties();
        try (InputStream is = AlarmConfig.class.getClassLoader().getResourceAsStream(classpathPropertiesFile))
        {
            if (is != null)
            {
                props.load(is);
            }
        }
        catch (IOException e)
        {
            throw new RuntimeException("Failed to load alarm config from " + classpathPropertiesFile, e);
        }

        String serverUrl = props.getProperty("alarm.serverUrl");
        if (serverUrl != null && !serverUrl.isEmpty())
        {
            config.setServerUrl(serverUrl);
        }
        String appId = props.getProperty("alarm.appId");
        if (appId != null && !appId.isEmpty())
        {
            config.setAppId(appId);
        }
        String appSecret = props.getProperty("alarm.appSecret");
        if (appSecret != null && !appSecret.isEmpty())
        {
            config.setAppSecret(appSecret);
        }
        String connectTimeout = props.getProperty("alarm.connectTimeout");
        if (connectTimeout != null && !connectTimeout.isEmpty())
        {
            config.setConnectTimeout(Integer.parseInt(connectTimeout));
        }
        String readTimeout = props.getProperty("alarm.readTimeout");
        if (readTimeout != null && !readTimeout.isEmpty())
        {
            config.setReadTimeout(Integer.parseInt(readTimeout));
        }

        return config;
    }

    public String getServerUrl()
    {
        return serverUrl;
    }

    public AlarmConfig setServerUrl(String serverUrl)
    {
        this.serverUrl = serverUrl;
        return this;
    }

    public String getAppId()
    {
        return appId;
    }

    public AlarmConfig setAppId(String appId)
    {
        this.appId = appId;
        return this;
    }

    public String getAppSecret()
    {
        return appSecret;
    }

    public AlarmConfig setAppSecret(String appSecret)
    {
        this.appSecret = appSecret;
        return this;
    }

    public int getConnectTimeout()
    {
        return connectTimeout;
    }

    public AlarmConfig setConnectTimeout(int connectTimeout)
    {
        this.connectTimeout = connectTimeout;
        return this;
    }

    public int getReadTimeout()
    {
        return readTimeout;
    }

    public AlarmConfig setReadTimeout(int readTimeout)
    {
        this.readTimeout = readTimeout;
        return this;
    }
}
