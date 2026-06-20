package com.ruoyi.alarm.sdk;

import com.alibaba.fastjson.JSON;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

/**
 * 预警平台 SDK 核心工具类
 * 封装 HTTP 调用预警平台 /notify/* 接口的全链路逻辑
 *
 * @author ruoyi
 */
public class AlarmSimpleUtil
{
    /** 预警平台配置 */
    private final AlarmConfig config;

    /**
     * 构造方法
     *
     * @param config 预警平台配置
     */
    public AlarmSimpleUtil(AlarmConfig config)
    {
        this.config = config;
    }

    /**
     * Builder 模式构建 AlarmSimpleUtil 实例
     *
     * @param config 预警平台配置
     * @return AlarmSimpleUtil 实例
     */
    public static AlarmSimpleUtil builder(AlarmConfig config)
    {
        return new AlarmSimpleUtil(config);
    }

    /**
     * 发送预警消息到 /notify/send 接口
     *
     * @param message 预警消息
     * @return 响应对象
     */
    public AlarmResponse send(AlarmMessage message)
    {
        try
        {
            String url = config.getServerUrl() + "/notify/send";
            String json = JSON.toJSONString(message);
            return post(url, json);
        }
        catch (Exception e)
        {
            AlarmResponse response = new AlarmResponse();
            response.setCode(-1);
            response.setMsg("Failed to send alarm message: " + e.getMessage());
            response.setData(null);
            return response;
        }
    }

    /**
     * 发送恢复通知到 /notify/recover 接口
     *
     * @param taskName 任务名称
     * @param idempotentValue 幂等值
     * @return 响应对象
     */
    public AlarmResponse sendRecoverMessage(String taskName, String idempotentValue)
    {
        try
        {
            String url = config.getServerUrl() + "/notify/recover";
            Map<String, Object> params = new HashMap<>();
            params.put("taskName", taskName);
            params.put("idempotentValue", idempotentValue);
            String json = JSON.toJSONString(params);
            return post(url, json);
        }
        catch (Exception e)
        {
            AlarmResponse response = new AlarmResponse();
            response.setCode(-1);
            response.setMsg("Failed to send recover message: " + e.getMessage());
            response.setData(null);
            return response;
        }
    }

    /**
     * 智能判断发送预警或恢复通知
     * 当 idempotentValue 不为空时先查询是否已有活跃预警，有则发恢复通知，无则发预警消息
     * idempotentValue 为空时直接发预警消息
     *
     * @param taskName 任务名称
     * @param idempotentValue 幂等值
     * @param alarmLevel 预警级别
     * @param alarmType 预警类型
     * @param templateVars 模板变量
     * @return 响应对象
     */
    public AlarmResponse sendOrRecover(String taskName, String idempotentValue, String alarmLevel, String alarmType, Map<String, String> templateVars)
    {
        if (idempotentValue == null || idempotentValue.isEmpty())
        {
            // 幂等值为空，直接发送预警消息
            return sendAlarmMessage(taskName, idempotentValue, alarmLevel, alarmType, templateVars);
        }

        // 幂等值不为空，先查询是否已有活跃预警
        boolean hasActiveAlarm = checkActiveAlarm(taskName, idempotentValue);
        if (hasActiveAlarm)
        {
            // 已有活跃预警，发送恢复通知
            return sendRecoverMessage(taskName, idempotentValue);
        }
        else
        {
            // 无活跃预警，发送预警消息
            return sendAlarmMessage(taskName, idempotentValue, alarmLevel, alarmType, templateVars);
        }
    }

    /**
     * 检查是否存在活跃预警
     *
     * @param taskName 任务名称
     * @param idempotentValue 幂等值
     * @return true 表示存在活跃预警
     */
    private boolean checkActiveAlarm(String taskName, String idempotentValue)
    {
        try
        {
            String url = config.getServerUrl() + "/notify/check";
            Map<String, Object> params = new HashMap<>();
            params.put("taskName", taskName);
            params.put("idempotentValue", idempotentValue);
            String json = JSON.toJSONString(params);
            AlarmResponse response = post(url, json);
            return response != null && response.isSuccess();
        }
        catch (Exception e)
        {
            // 查询失败，默认认为无活跃预警
            return false;
        }
    }

    /**
     * 发送预警消息
     *
     * @param taskName 任务名称
     * @param idempotentValue 幂等值
     * @param alarmLevel 预警级别
     * @param alarmType 预警类型
     * @param templateVars 模板变量
     * @return 响应对象
     */
    private AlarmResponse sendAlarmMessage(String taskName, String idempotentValue, String alarmLevel, String alarmType, Map<String, String> templateVars)
    {
        try
        {
            String url = config.getServerUrl() + "/notify/send";
            Map<String, Object> params = new HashMap<>();
            params.put("taskName", taskName);
            if (idempotentValue != null && !idempotentValue.isEmpty())
            {
                params.put("idempotentValue", idempotentValue);
            }
            if (alarmLevel != null && !alarmLevel.isEmpty())
            {
                params.put("alarmLevel", alarmLevel);
            }
            if (alarmType != null && !alarmType.isEmpty())
            {
                params.put("alarmType", alarmType);
            }
            if (templateVars != null && !templateVars.isEmpty())
            {
                params.put("templateVars", templateVars);
            }
            String json = JSON.toJSONString(params);
            return post(url, json);
        }
        catch (Exception e)
        {
            AlarmResponse response = new AlarmResponse();
            response.setCode(-1);
            response.setMsg("Failed to send alarm message: " + e.getMessage());
            response.setData(null);
            return response;
        }
    }

    /**
     * 发送 POST 请求
     *
     * @param url 请求 URL
     * @param json 请求体 JSON
     * @return 响应对象
     */
    private AlarmResponse post(String url, String json) throws IOException
    {
        HttpURLConnection connection = null;
        try
        {
            URL urlObj = new URL(url);
            connection = (HttpURLConnection) urlObj.openConnection();
            connection.setRequestMethod("POST");
            connection.setConnectTimeout(config.getConnectTimeout());
            connection.setReadTimeout(config.getReadTimeout());
            connection.setDoOutput(true);
            connection.setDoInput(true);
            connection.setRequestProperty("Content-Type", "application/json");
            connection.setRequestProperty("X-App-Id", config.getAppId());
            connection.setRequestProperty("X-App-Secret", config.getAppSecret());

            try (OutputStream os = connection.getOutputStream())
            {
                os.write(json.getBytes(StandardCharsets.UTF_8));
            }

            int responseCode = connection.getResponseCode();
            try (InputStream is = connection.getInputStream())
            {
                BufferedReader reader = new BufferedReader(new InputStreamReader(is, StandardCharsets.UTF_8));
                StringBuilder sb = new StringBuilder();
                String line;
                while ((line = reader.readLine()) != null)
                {
                    sb.append(line);
                }
                return AlarmResponse.parse(sb.toString());
            }
        }
        catch (Exception e)
        {
            AlarmResponse response = new AlarmResponse();
            response.setCode(-1);
            response.setMsg("HTTP request failed: " + e.getMessage());
            response.setData(null);
            return response;
        }
        finally
        {
            if (connection != null)
            {
                connection.disconnect();
            }
        }
    }
}
