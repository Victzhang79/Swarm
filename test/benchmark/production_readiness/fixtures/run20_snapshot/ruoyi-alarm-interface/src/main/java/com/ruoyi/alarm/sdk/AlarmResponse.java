package com.ruoyi.alarm.sdk;

import com.alibaba.fastjson.JSON;

/**
 * 预警平台统一响应封装类
 *
 * @author ruoyi
 */
public class AlarmResponse
{
    /** 响应码 */
    private int code;

    /** 响应消息 */
    private String msg;

    /** 响应数据 */
    private Object data;

    /**
     * 解析预警平台 HTTP 返回 JSON
     *
     * @param json JSON 字符串
     * @return 响应对象
     */
    public static AlarmResponse parse(String json)
    {
        return JSON.parseObject(json, AlarmResponse.class);
    }

    /**
     * 判断响应是否成功（code == 200）
     *
     * @return true 表示成功
     */
    public boolean isSuccess()
    {
        return code == 200;
    }

    public int getCode()
    {
        return code;
    }

    public void setCode(int code)
    {
        this.code = code;
    }

    public String getMsg()
    {
        return msg;
    }

    public void setMsg(String msg)
    {
        this.msg = msg;
    }

    public Object getData()
    {
        return data;
    }

    public void setData(Object data)
    {
        this.data = data;
    }
}
